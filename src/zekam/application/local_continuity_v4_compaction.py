"""Dormant, generation-owned contracts for atomic V4 pre-compaction."""

# ruff: noqa: SIM905 -- compact exact vocabularies keep the fixed file cap.

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NoReturn, SupportsIndex, TypeVar, final
from weakref import WeakValueDictionary

from zekam.application.local_continuity import ContinuityBinding, digest_text, timestamp, uuid_text
from zekam.application.local_continuity_v4_writer import CurrentSourceSnapshot
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

PRECOMPACT_TOTAL_BUDGET_MS: Final = 8_000
PRECOMPACT_FINAL_RESERVE_MS: Final = 1_500
_SUCCESS: Final = b'{"continue":true,"suppressOutput":true}\n'
_SUCCESS_DIGEST: Final = "sha256:83b0c2d644685886e897a47420a509055cd62bdc37be550ee96b839cdb1028be"
_DEADLINES: WeakValueDictionary[str, SealedPreCompactionDeadline] = WeakValueDictionary()
_PLANS: WeakValueDictionary[str, PreparedPreCompactionPlan] = WeakValueDictionary()
_DECISIONS: WeakValueDictionary[str, VerifiedAckDecision] = WeakValueDictionary()
_RESULTS: WeakValueDictionary[str, PreCompactionResult] = WeakValueDictionary()
_PARITY: dict[str, bytes] = {}
_T = TypeVar("_T")


@final
@dataclass(frozen=True, slots=True)
class ResolvedPreCompactionBinding:
    """Read-only locator result; the writer must re-read every durable coordinate."""

    binding: ContinuityBinding
    attachment_id: str
    head_revision_digest: str
    head_state: str
    active_manifest_digest: str
    active_hydration_receipt_digest: str
    resolution_digest: str

    def __post_init__(self) -> None:
        if type(self.binding) is not ContinuityBinding:
            raise ValidationFailed("PreCompact exact resolved binding required")
        self.binding.__post_init__()
        uuid_text(self.attachment_id, "PreCompact attachment")
        for value in (
            self.head_revision_digest,
            self.active_manifest_digest,
            self.active_hydration_receipt_digest,
            self.resolution_digest,
        ):
            digest_text(value)
        if self.head_state not in {"hydrated", "pre-compact-committed"}:
            raise PolicyViolation("PreCompact resolved attachment state invalid")
        body = {
            "schema": "zekam-precompact-existing-binding-resolution/v1",
            "binding_digest": self.binding.binding_digest,
            "attachment_id": self.attachment_id,
            "head_revision_digest": self.head_revision_digest,
            "head_state": self.head_state,
            "active_manifest_digest": self.active_manifest_digest,
            "active_hydration_receipt_digest": self.active_hydration_receipt_digest,
        }
        if digest(body) != self.resolution_digest:
            raise PolicyViolation("PreCompact resolved binding digest mismatch")


def _generation_digest(generation: object) -> str:
    from zekam.infrastructure.macos_precompaction_supervisor import _generation_digest_if_current

    return _generation_digest_if_current(generation)


def _value_bytes(value: object) -> bytes:
    fields = getattr(type(value), "__dataclass_fields__", {})
    body: dict[str, object] = {}
    for name in fields:
        if name == "_seal":
            continue
        item = getattr(value, name)
        if isinstance(item, bytes):
            item = item.decode("utf-8")
        elif isinstance(item, ContinuityBinding):
            item = item.binding_digest
        elif isinstance(item, CurrentSourceSnapshot):
            item = item.snapshot_digest
        body[name] = item
    return canonical_json(body).encode("utf-8")


def _registered(  # noqa: UP047 -- runtime also supports Python 3.11
    value: _T, registry: WeakValueDictionary[str, _T]
) -> None:
    seal = getattr(value, "_seal", None)
    if type(seal) is not str or registry.get(seal) is not value:
        raise PolicyViolation("PreCompact unissued sealed value")
    if _PARITY.get(seal) != _value_bytes(value):
        raise PolicyViolation("PreCompact sealed value changed")


def _pickle_blocked() -> NoReturn:
    raise TypeError("PreCompact sealed values are not serializable")


class PreCompactionFailure(StrEnum):
    VALIDATION = "VALIDATION"
    PENDING_WORK = "PENDING_WORK"
    UNPERSISTED_DELTA = "UNPERSISTED_DELTA"
    SOURCE_DRIFT = "SOURCE_DRIFT"
    PROCESS_DRIFT = "PROCESS_DRIFT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    DEADLINE = "DEADLINE"


def _failure_bytes(category: PreCompactionFailure) -> bytes:
    return (
        canonical_json(
            {
                "continue": False,
                "stopReason": f"ZEKAM_PRECOMPACT_{category.value}",
                "suppressOutput": True,
            }
        ).encode("utf-8")
        + b"\n"
    )


@final
class SealedPreCompactionDeadline:
    __slots__ = ("__weakref__", "_clock", "_deadline_ns", "_generation_digest", "_seal")
    _clock: Callable[[], int]
    _deadline_ns: int
    _generation_digest: str
    _seal: str

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("PreCompact deadlines are service-generation owned")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("PreCompact deadline is final")

    def remaining_ns(self) -> int:
        _registered(self, _DEADLINES)
        now = self._clock()
        if type(now) is not int or now < 0:
            raise PolicyViolation("PreCompact trusted clock drift")
        return max(0, self._deadline_ns - now)

    def remaining_seconds(self, *, reserve_ms: int = 0) -> float:
        if type(reserve_ms) is not int or reserve_ms < 0:
            raise ValidationFailed("PreCompact exact deadline reserve required")
        remaining = self.remaining_ns() - reserve_ms * 1_000_000
        if remaining <= 0:
            raise TimeoutError("PreCompact deadline exhausted")
        return remaining / 1_000_000_000

    def require_current(self, *, reserve_ms: int = 0) -> None:
        self.remaining_seconds(reserve_ms=reserve_ms)

    def assert_generation(self, generation: object) -> None:
        if _generation_digest(generation) != self._generation_digest:
            raise PolicyViolation("PreCompact deadline generation drift")
        self.require_current()

    def __reduce__(self) -> NoReturn:
        _pickle_blocked()

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        _pickle_blocked()


def _issue_deadline(generation: object, clock: Callable[[], int]) -> SealedPreCompactionDeadline:
    generation_digest = _generation_digest(generation)
    start = clock()
    if type(start) is not int or start < 0:
        raise ValidationFailed("PreCompact monotonic clock invalid")
    value = object.__new__(SealedPreCompactionDeadline)
    seal = secrets.token_hex(32)
    object.__setattr__(value, "_clock", clock)
    object.__setattr__(value, "_deadline_ns", start + PRECOMPACT_TOTAL_BUDGET_MS * 1_000_000)
    object.__setattr__(value, "_generation_digest", generation_digest)
    object.__setattr__(value, "_seal", seal)
    _DEADLINES[seal] = value
    _PARITY[seal] = _value_bytes(value)
    return value


@final
@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class PreparedPreCompactionPlan:
    _seal: str
    generation_digest: str
    binding: ContinuityBinding
    observed_at: str
    delivery_id: str
    spool_entry_digest: str
    spool_entry_digests: tuple[str, ...]
    source_snapshot: CurrentSourceSnapshot
    predecessor_revision_digest: str
    process_generation_digest: str
    active_manifest_digest: str
    active_hydration_receipt_digest: str
    old_sequence: int
    old_event_digest: str
    ancestry_body_json: str
    ancestry_receipt_digest: str
    native_body_json: str
    native_receipt_digest: str
    checkpoint_event_id: str
    checkpoint_event_body_json: str
    checkpoint_event_digest: str
    checkpoint_receipt_body_json: str
    checkpoint_receipt_digest: str
    precompact_event_id: str
    precompact_event_body_json: str
    precompact_event_digest: str
    checkpoint_body_json: str
    checkpoint_digest: str
    revision_body_json: str
    revision_digest: str
    ack_body_json: str
    ack_decision_digest: str
    rows: tuple[tuple[str, tuple[object, ...]], ...]

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("PreCompact plans are service-generation owned")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("PreCompact plan is final")

    def __post_init__(self) -> None:
        _registered(self, _PLANS)
        digest_text(self.generation_digest)
        self.binding.__post_init__()
        checked = timestamp(self.observed_at)
        if checked != self.observed_at or "." in checked or not checked.endswith("+00:00"):
            raise ValidationFailed("PreCompact whole-second UTC required")
        if type(self.old_sequence) is not int or self.old_sequence < 1:
            raise ValidationFailed("PreCompact old sequence required")
        for item in (
            self.delivery_id,
            self.spool_entry_digest,
            *self.spool_entry_digests,
            self.predecessor_revision_digest,
            self.process_generation_digest,
            self.active_manifest_digest,
            self.active_hydration_receipt_digest,
            self.old_event_digest,
            self.ancestry_receipt_digest,
            self.native_receipt_digest,
            self.checkpoint_event_digest,
            self.checkpoint_receipt_digest,
            self.precompact_event_digest,
            self.checkpoint_digest,
            self.revision_digest,
            self.ack_decision_digest,
        ):
            digest_text(item)
        for encoded, expected in (
            (self.ancestry_body_json, self.ancestry_receipt_digest),
            (self.native_body_json, self.native_receipt_digest),
            (self.checkpoint_event_body_json, self.checkpoint_event_digest),
            (self.precompact_event_body_json, self.precompact_event_digest),
            (self.checkpoint_body_json, self.checkpoint_digest),
            (self.revision_body_json, self.revision_digest),
            (self.ack_body_json, self.ack_decision_digest),
        ):
            if type(encoded) is not str or not 1 <= len(encoded.encode("utf-8")) <= 1_048_576:
                raise ValidationFailed("PreCompact canonical body outside bound")
            body = json.loads(encoded)
            if (
                type(body) is not dict
                or canonical_json(body) != encoded
                or digest(body) != expected
            ):
                raise PolicyViolation("PreCompact body/digest parity drift")
        if type(self.rows) is not tuple or not self.rows:
            raise ValidationFailed("PreCompact immutable row plan required")

    def __reduce__(self) -> NoReturn:
        _pickle_blocked()

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        _pickle_blocked()


def _issue_plan(generation: object, values: Mapping[str, object]) -> PreparedPreCompactionPlan:
    generation_digest = _generation_digest(generation)
    expected = set(PreparedPreCompactionPlan.__dataclass_fields__) - {"_seal", "generation_digest"}
    if type(values) is not dict or set(values) != expected:
        raise ValidationFailed("PreCompact exact plan fields required")
    result = object.__new__(PreparedPreCompactionPlan)
    seal = secrets.token_hex(32)
    object.__setattr__(result, "generation_digest", generation_digest)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    object.__setattr__(result, "_seal", seal)
    _PLANS[seal] = result
    _PARITY[seal] = _value_bytes(result)
    result.__post_init__()
    return result


@final
@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class VerifiedAckDecision:
    _seal: str
    generation_digest: str
    body_json: str
    decision_digest: str
    checkpoint_digest: str
    checkpoint_requested_event_digest: str
    pre_compaction_event_digest: str
    native_receipt_digest: str
    attachment_revision_digest: str

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("PreCompact ACK decisions are durable-verifier owned")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("PreCompact ACK decision is final")

    def __post_init__(self) -> None:
        _registered(self, _DECISIONS)
        body = json.loads(self.body_json)
        if (
            type(body) is not dict
            or canonical_json(body) != self.body_json
            or digest(body) != self.decision_digest
            or body.get("success_stdout_digest") != _SUCCESS_DIGEST
        ):
            raise PolicyViolation("PreCompact ACK decision relation drift")

    def __reduce__(self) -> NoReturn:
        _pickle_blocked()

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        _pickle_blocked()


@final
@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class PreCompactionResult:
    _seal: str
    _stdout: bytes
    status: str
    failure_category: str | None
    checkpoint_digest: str | None
    checkpoint_requested_event_digest: str | None
    pre_compaction_event_digest: str | None
    native_receipt_digest: str | None
    attachment_revision_digest: str | None
    ack_decision_digest: str | None
    replay: bool
    durable_reopen_verified: bool
    native_ack_observed: bool
    grants_authority: bool

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("PreCompact results are factory owned")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("PreCompact result is final")

    def __post_init__(self) -> None:
        _registered(self, _RESULTS)
        flags = (
            self.replay,
            self.durable_reopen_verified,
            self.native_ack_observed,
            self.grants_authority,
        )
        if tuple(type(item) for item in flags) != (bool,) * 4 or any(flags[2:]):
            raise PolicyViolation("PreCompact result authority relation invalid")
        graph = (
            self.checkpoint_digest,
            self.checkpoint_requested_event_digest,
            self.pre_compaction_event_digest,
            self.native_receipt_digest,
            self.attachment_revision_digest,
            self.ack_decision_digest,
        )
        if self.status == "checkpoint-ready":
            if (
                self.failure_category is not None
                or not self.durable_reopen_verified
                or self._stdout != _SUCCESS
            ):
                raise PolicyViolation("PreCompact success result relation invalid")
            for item in graph:
                digest_text(item)
            return
        try:
            category = PreCompactionFailure(self.failure_category or "")
        except (ValueError, TypeError) as exc:
            raise ValidationFailed("PreCompact failure category invalid") from exc
        if (
            self.status not in {"rejected", "recovery-required"}
            or self.replay
            or any(item is not None for item in graph)
            or self.durable_reopen_verified
            or self._stdout != _failure_bytes(category)
        ):
            raise PolicyViolation("PreCompact failure result relation invalid")

    @property
    def stdout(self) -> bytes:
        self.__post_init__()
        return memoryview(self._stdout).tobytes()

    def __reduce__(self) -> NoReturn:
        _pickle_blocked()

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        _pickle_blocked()


def _new_result(values: tuple[object, ...]) -> PreCompactionResult:
    result = object.__new__(PreCompactionResult)
    seal = secrets.token_hex(32)
    for name, value in zip(
        tuple(PreCompactionResult.__dataclass_fields__)[1:], values, strict=True
    ):
        object.__setattr__(result, name, value)
    object.__setattr__(result, "_seal", seal)
    _RESULTS[seal] = result
    _PARITY[seal] = _value_bytes(result)
    result.__post_init__()
    return result


def _issue_ack_decision(generation: object, body: Mapping[str, object]) -> VerifiedAckDecision:
    generation_digest = _generation_digest(generation)
    required = frozenset(
        (
            "active_hydration_receipt_digest active_manifest_digest ancestry_receipt_digest "
            "approval_inherited attachment_id binding_digest checkpoint_digest "
            "checkpoint_requested_event_digest client_id delivery_id device_id "
            "durable_reopen_verified external_session_id full_spool_tuple_digest "
            "grants_authority hydrated_predecessor_revision_digest internal_receipt_digest "
            "native_ack_observed native_receipt_digest pre_compact_committed_revision_digest "
            "pre_compaction_event_digest process_generation_digest schema session_id "
            "source_revision source_snapshot_digest source_snapshot_id spool_entry_digest "
            "success_stdout_digest"
        ).split()
    )
    if type(body) is not dict or set(body) != required:
        raise ValidationFailed("PreCompact exact ACK decision body required")
    if body["schema"] != "zekam-precompaction-ack-decision/v1" or any(
        body[name] is not expected
        for name, expected in (
            ("durable_reopen_verified", True),
            ("native_ack_observed", False),
            ("grants_authority", False),
            ("approval_inherited", False),
        )
    ):
        raise PolicyViolation("PreCompact ACK authority flags invalid")
    encoded = canonical_json(body)
    result = object.__new__(VerifiedAckDecision)
    seal = secrets.token_hex(32)
    values = (
        generation_digest,
        encoded,
        digest(body),
        body["checkpoint_digest"],
        body["checkpoint_requested_event_digest"],
        body["pre_compaction_event_digest"],
        body["native_receipt_digest"],
        body["pre_compact_committed_revision_digest"],
    )
    for name, value in zip(
        tuple(VerifiedAckDecision.__dataclass_fields__)[1:], values, strict=True
    ):
        object.__setattr__(result, name, value)
    object.__setattr__(result, "_seal", seal)
    _DECISIONS[seal] = result
    _PARITY[seal] = _value_bytes(result)
    result.__post_init__()
    return result


def _checkpoint_ready(
    generation: object, decision: VerifiedAckDecision, *, replay: bool
) -> PreCompactionResult:
    if type(decision) is not VerifiedAckDecision or type(replay) is not bool:
        raise ValidationFailed("PreCompact exact verified decision required")
    decision.__post_init__()
    if _generation_digest(generation) != decision.generation_digest:
        raise PolicyViolation("PreCompact result generation drift")
    return _new_result(
        (
            _SUCCESS,
            "checkpoint-ready",
            None,
            decision.checkpoint_digest,
            decision.checkpoint_requested_event_digest,
            decision.pre_compaction_event_digest,
            decision.native_receipt_digest,
            decision.attachment_revision_digest,
            decision.decision_digest,
            replay,
            True,
            False,
            False,
        )
    )


def checkpoint_ready(*_values: object, **_named: object) -> NoReturn:
    raise PolicyViolation("PreCompact success is durable-verifier owned")


def issue_ack_decision(*_values: object, **_named: object) -> NoReturn:
    raise PolicyViolation("PreCompact ACK decisions are durable-verifier owned")


def rejected(category: PreCompactionFailure) -> PreCompactionResult:
    if type(category) is not PreCompactionFailure:
        raise ValidationFailed("PreCompact exact failure category required")
    return _new_result(
        (
            _failure_bytes(category),
            "rejected",
            category.value,
            *(None,) * 6,
            False,
            False,
            False,
            False,
        )
    )


def recovery_required(category: PreCompactionFailure) -> PreCompactionResult:
    if type(category) is not PreCompactionFailure:
        raise ValidationFailed("PreCompact exact failure category required")
    return _new_result(
        (
            _failure_bytes(category),
            "recovery-required",
            category.value,
            *(None,) * 6,
            False,
            False,
            False,
            False,
        )
    )
