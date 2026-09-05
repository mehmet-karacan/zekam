"""Dormant WP-08 B1 contracts for trusted internal continuity producers.

This module deliberately exposes no production issuer or commitment oracle.  A
future approved composition owns the fixed issuer handles.  The SQLite adapter
accepts only the immutable snapshots those handles return.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from zekam.application.local_continuity import (
    ContinuityBinding,
    ContinuityTail,
    digest_text,
    logical,
    timestamp,
    uuid_text,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed

EVENT_NS = UUID("8f950d20-9db0-5d5e-9f54-99a7987ebf4b")

_ITEM = re.compile(r"turn/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MAX_TEXT_BYTES = 512


def _binding(value: object) -> ContinuityBinding:
    if type(value) is not ContinuityBinding:
        raise ValidationFailed("B1 exact continuity binding required")
    value.__post_init__()
    return value


def _tail(value: object) -> ContinuityTail:
    if type(value) is not ContinuityTail:
        raise ValidationFailed("B1 exact continuity tail required")
    value.__post_init__()
    if type(value.sequence) is not int or (
        value.event_digest is not None and type(value.event_digest) is not str
    ):
        raise ValidationFailed("B1 exact continuity tail fields required")
    return value


def _text(value: object, label: str, *, maximum: int = _MAX_TEXT_BYTES) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"{label} exact string required")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationFailed(f"{label} valid UTF-8 required") from exc
    if not encoded or len(encoded) > maximum or value != value.strip():
        raise ValidationFailed(f"{label} outside canonical bound")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationFailed(f"{label} contains control characters")
    return value


def _key(value: object, label: str) -> str:
    result = _text(value, label)
    if logical(result, label) != result:
        raise ValidationFailed(f"{label} is not canonical")
    return result


def _digest(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"{label} exact digest string required")
    digest_text(value)
    return value


def _uuid(value: object, label: str) -> str:
    result = _text(value, label)
    uuid_text(result, label)
    if str(UUID(result)) != result:
        raise ValidationFailed(f"{label} canonical lowercase UUID required")
    return result


def _runtime_time(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"{label} exact timestamp string required")
    result = timestamp(value)
    parsed = datetime.fromisoformat(result)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValidationFailed(f"{label} canonical UTC timestamp required")
    return result


def _issued_time(value: object, label: str) -> str:
    result = _runtime_time(value, label)
    if len(result) != 25 or not result.endswith("+00:00") or "." in result:
        raise ValidationFailed(f"{label} whole-second UTC required")
    return result


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ValidationFailed(f"{label} exact positive integer required")
    return value


@dataclass(frozen=True, slots=True)
class TurnCommitRequest:
    binding: ContinuityBinding
    role: Literal["user", "assistant"]
    item_ref: str
    expected_tail: ContinuityTail

    def __post_init__(self) -> None:
        _binding(self.binding)
        role: object = self.role
        if type(role) is not str or role not in {"user", "assistant"}:
            raise ValidationFailed("B1 exact turn role required")
        if type(self.item_ref) is not str or _ITEM.fullmatch(self.item_ref) is None:
            raise ValidationFailed("B1 exact turn/<canonical UUID> item ref required")
        _uuid(self.item_ref.removeprefix("turn/"), "B1 turn item UUID")
        _tail(self.expected_tail)


@dataclass(frozen=True, slots=True)
class EffectClaimRequest:
    binding: ContinuityBinding
    job_id: str
    expected_tail: ContinuityTail

    def __post_init__(self) -> None:
        _binding(self.binding)
        _uuid(self.job_id, "B1 selected job")
        _tail(self.expected_tail)


@dataclass(frozen=True, slots=True)
class DirectEffectOutcomeRequest:
    binding: ContinuityBinding
    claim_id: str
    expected_tail: ContinuityTail

    def __post_init__(self) -> None:
        _binding(self.binding)
        _uuid(self.claim_id, "B1 selected claim")
        _tail(self.expected_tail)


@dataclass(frozen=True, slots=True, init=False)
class FrozenTurnCommitSnapshot:
    binding_digest: str
    role: str
    item_ref: str
    content_commitment_digest: str
    store_generation_commitment_digest: str
    previous_store_generation_commitment_digest: str | None
    issuer_receipt_id: str
    committed_at: str

    def __new__(cls) -> FrozenTurnCommitSnapshot:
        raise TypeError("B1 turn snapshots require a fixed issuer")

    def __post_init__(self) -> None:
        _digest(self.binding_digest, "B1 turn binding")
        if type(self.role) is not str or self.role not in {"user", "assistant"}:
            raise ValidationFailed("B1 exact frozen turn role required")
        if type(self.item_ref) is not str or _ITEM.fullmatch(self.item_ref) is None:
            raise ValidationFailed("B1 exact frozen item ref required")
        _digest(self.content_commitment_digest, "B1 turn content commitment")
        _digest(self.store_generation_commitment_digest, "B1 turn generation commitment")
        if self.previous_store_generation_commitment_digest is not None:
            _digest(
                self.previous_store_generation_commitment_digest,
                "B1 previous generation commitment",
            )
        _key(self.issuer_receipt_id, "B1 turn issuer receipt")
        _issued_time(self.committed_at, "B1 turn committed_at")


@dataclass(frozen=True, slots=True, init=False)
class FrozenEffectClaimSnapshot:
    binding_digest: str
    job_id: str
    job_state: str
    job_payload_digest: str
    job_updated_at: str
    lease_id: str
    lease_owner_id: str
    lease_owner_pid: int
    lease_owner_token: str
    fencing_token: int
    lease_heartbeat_at: str
    lease_expires_at: str
    resource_locks: tuple[tuple[str, str], ...]
    operation: str
    effect_commitment_digest: str
    claimed_at: str

    def __new__(cls) -> FrozenEffectClaimSnapshot:
        raise TypeError("B1 claim snapshots require a fixed issuer")

    def __post_init__(self) -> None:
        _digest(self.binding_digest, "B1 claim binding")
        _uuid(self.job_id, "B1 claim job")
        if type(self.job_state) is not str or self.job_state != "running":
            raise ValidationFailed("B1 claim exact running job required")
        _digest(self.job_payload_digest, "B1 job payload")
        _runtime_time(self.job_updated_at, "B1 job updated_at")
        _uuid(self.lease_id, "B1 lease")
        _key(self.lease_owner_id, "B1 lease owner")
        _positive_int(self.lease_owner_pid, "B1 lease owner PID")
        _key(self.lease_owner_token, "B1 lease owner token")
        _positive_int(self.fencing_token, "B1 fencing token")
        _runtime_time(self.lease_heartbeat_at, "B1 lease heartbeat")
        _runtime_time(self.lease_expires_at, "B1 lease expiry")
        if type(self.resource_locks) is not tuple or len(self.resource_locks) > 64:
            raise ValidationFailed("B1 exact bounded resource lock tuple required")
        checked: list[tuple[str, str]] = []
        for item in self.resource_locks:
            if type(item) is not tuple or len(item) != 2:
                raise ValidationFailed("B1 exact resource lock pair required")
            resource = _key(item[0], "B1 resource")
            acquired = _runtime_time(item[1], "B1 lock acquired_at")
            checked.append((resource, acquired))
        if (
            tuple(checked) != self.resource_locks
            or tuple(sorted(checked)) != self.resource_locks
            or len({item[0] for item in checked}) != len(checked)
        ):
            raise ValidationFailed("B1 resource locks must be unique canonical order")
        _key(self.operation, "B1 effect operation")
        _digest(self.effect_commitment_digest, "B1 effect commitment")
        _issued_time(self.claimed_at, "B1 effect claimed_at")


@dataclass(frozen=True, slots=True, init=False)
class FrozenDirectEffectOutcomeSnapshot:
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
    status: str
    outcome_commitment_digest: str
    completed_at: str

    def __new__(cls) -> FrozenDirectEffectOutcomeSnapshot:
        raise TypeError("B1 outcome snapshots require a fixed issuer")

    def __post_init__(self) -> None:
        _digest(self.binding_digest, "B1 outcome binding")
        _uuid(self.job_id, "B1 outcome job")
        _uuid(self.claim_id, "B1 outcome claim")
        _uuid(self.lease_id, "B1 outcome lease")
        _key(self.lease_owner_id, "B1 outcome lease owner")
        _positive_int(self.lease_owner_pid, "B1 outcome owner PID")
        _key(self.lease_owner_token, "B1 outcome owner token")
        _positive_int(self.fencing_token, "B1 outcome fencing token")
        _key(self.operation, "B1 outcome operation")
        _digest(self.effect_commitment_digest, "B1 outcome effect commitment")
        _issued_time(self.claimed_at, "B1 outcome claimed_at")
        if type(self.status) is not str or self.status not in {"completed", "failed"}:
            raise ValidationFailed("B1 direct outcome status unsupported")
        _digest(self.outcome_commitment_digest, "B1 outcome commitment")
        _issued_time(self.completed_at, "B1 effect completed_at")


class TurnCommitIssuer(Protocol):
    def snapshot(self, request: TurnCommitRequest) -> FrozenTurnCommitSnapshot: ...

    def recheck(self, snapshot: FrozenTurnCommitSnapshot) -> None: ...


class EffectClaimIssuer(Protocol):
    def snapshot(self, request: EffectClaimRequest) -> FrozenEffectClaimSnapshot: ...

    def recheck(self, snapshot: FrozenEffectClaimSnapshot) -> None: ...


class DirectEffectOutcomeIssuer(Protocol):
    def snapshot(
        self, request: DirectEffectOutcomeRequest
    ) -> FrozenDirectEffectOutcomeSnapshot: ...

    def recheck(self, snapshot: FrozenDirectEffectOutcomeSnapshot) -> None: ...


@dataclass(frozen=True, slots=True)
class InternalProducerResult:
    event_kind: str
    event_digest: str
    producer_ref: str
    replay: bool

    def __post_init__(self) -> None:
        if type(self.event_kind) is not str or self.event_kind not in {
            "USER_TURN_COMMITTED",
            "ASSISTANT_TURN_COMMITTED",
            "TOOL_EFFECT_CLAIMED",
            "TOOL_EFFECT_COMPLETED",
        }:
            raise ValidationFailed("B1 result event kind unsupported")
        _digest(self.event_digest, "B1 result event")
        _key(self.producer_ref, "B1 result producer ref")
        if type(self.replay) is not bool:
            raise ValidationFailed("B1 result exact replay bool required")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-v4-internal-producer-result/v1",
            "event_kind": self.event_kind,
            "event_digest": self.event_digest,
            "producer_ref": self.producer_ref,
            "replay": self.replay,
            "runtime_job_terminal_written": False,
            "terminal_outbox_written": False,
            "grants_authority": False,
        }

    @property
    def result_digest(self) -> str:
        return digest(self.body())
