"""Durable, authority-free local spool for client lifecycle observations.

The command-hook process must not depend on PostgreSQL or a provider being
available.  It appends a content-free observation to this immutable local
outbox and returns.  A separate governed worker may later replay pending
entries and add an immutable acknowledgement; an acknowledgement never edits
or removes the source event.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from zekam.domain.canonical import canonical_bytes, digest, parse_digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    PolicyViolation,
    ValidationFailed,
    ZekamError,
)

SPOOL_ENTRY_SCHEMA = "zekam-client-lifecycle-spool-entry/v1"
SPOOL_ACK_SCHEMA = "zekam-client-lifecycle-spool-ack/v1"
SPOOL_ATTEMPT_SCHEMA = "zekam-client-lifecycle-spool-attempt/v3"
SPOOL_ATTEMPT_STATE_SCHEMA = "zekam-client-lifecycle-attempt-state/v3"
SPOOL_DELIVERY_REF_SCHEMA = "zekam-client-lifecycle-delivery-ref/v1"
SPOOL_SESSION_CHECKPOINT_SCHEMA = "zekam-client-lifecycle-session-checkpoint/v1"
SPOOL_INSTANCE_SCHEMA = "zekam-client-lifecycle-instance/v1"
SPOOL_QUEUE_REF_SCHEMA = "zekam-client-lifecycle-queue-ref/v1"
SPOOL_QUEUE_STATE_SCHEMA = "zekam-client-lifecycle-queue-state/v1"
SPOOL_DRAIN_CURSOR_SCHEMA = "zekam-client-lifecycle-drain-cursor-record/v2"
SPOOL_DRAIN_CURSOR_POINTER_SCHEMA = "zekam-client-lifecycle-drain-cursor-pointer/v2"
CONTINUITY_BINDING_SCHEMA = "zekam-client-lifecycle-continuity-binding/v1"
CONTINUITY_PREFLIGHT_SCHEMA = "zekam-client-lifecycle-continuity-preflight/v1"
CANONICAL_LIFECYCLE_EVENT_SCHEMA = "zekam-client-lifecycle-event/v1"
MAX_PENDING_BATCH = 256
MAX_REPLAY_FAILURES = 3
MAX_SPOOL_DOCUMENT_BYTES = 1_048_576
MAX_LOCK_BYTES = 1
_WINDOWS_REPARSE_POINT = 0x0400
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_OPEN_BINARY = getattr(os, "O_BINARY", 0)

_LEDGER_EVENT_TYPES = {
    "session_start": "session.created",
    "pre_compaction": "session.compacting",
    "post_compaction": "session.compacted",
    "pre_close": "session.status",
    "post_close": "session.deleted",
}

_OBSERVATION_KEYS = frozenset(
    {
        "schema",
        "client_id",
        "client_kind",
        "client_version",
        "session_id",
        "external_event_type",
        "internal_event_type",
        "turn_id",
        "source",
        "trigger",
        "reason",
        "stop_hook_active",
        "permission_mode",
        "wire_digest",
        "contains_prompt",
        "contains_response",
        "contains_transcript",
        "grants_authority",
    }
)
_FALSE_KEYS = (
    "contains_prompt",
    "contains_response",
    "contains_transcript",
    "grants_authority",
)
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
_CLIENT_INSTANCE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_ENTRY_KEYS = frozenset(
    {
        "schema",
        "entry_digest",
        "delivery_id",
        "client_id",
        "client_kind",
        "client_version",
        "session_id",
        "sequence",
        "previous_entry_digest",
        "external_event_type",
        "internal_event_type",
        "observation_digest",
        "observation",
        "occurred_at",
        "grants_authority",
    }
)
_ACK_KEYS = frozenset(
    {
        "schema",
        "entry_digest",
        "canonical_event_digest",
        "canonical_event_id",
        "canonical_ack_digest",
        "canonical_lookup_digest",
        "runtime_binding_id",
        "runtime_binding_digest",
        "continuity_binding",
        "acknowledged_at",
        "grants_authority",
        "ack_digest",
    }
)
_CONTINUITY_BINDING_KEYS = frozenset(
    {
        "schema",
        "entry_digest",
        "canonical_event_digest",
        "realm_id",
        "project_id",
        "work_item_id",
        "run_id",
        "authorization_id",
        "job_id",
        "claim_id",
        "plan_digest",
        "effect_digest",
        "effect_receipt_id",
        "effect_receipt_digest",
        "continuity_event_id",
        "continuity_event_digest",
        "delivery_outbox_id",
        "terminal_receipt_digest",
        "event_type",
        "session_id",
        "client_id",
        "compiler_enqueue",
        "status",
        "grants_authority",
        "binding_digest",
    }
)
_CONTINUITY_PREFLIGHT_KEYS = frozenset(
    {
        "schema",
        "entry_digest",
        "canonical_event_digest",
        "client_instance_id",
        "realm_id",
        "project_id",
        "work_item_id",
        "run_id",
        "authorization_id",
        "job_id",
        "claim_id",
        "plan_digest",
        "effect_digest",
        "allowed",
        "mutation_performed",
        "grants_authority",
        "preflight_digest",
    }
)
_DELIVERY_REF_KEYS = frozenset(
    {
        "schema",
        "delivery_id",
        "entry_digest",
        "observation_digest",
        "client_id",
        "session_id",
        "queue_sequence",
        "grants_authority",
        "ref_digest",
    }
)
_QUEUE_REF_KEYS = frozenset(
    {
        "schema",
        "queue_sequence",
        "entry_digest",
        "previous_queue_entry_digest",
        "grants_authority",
        "ref_digest",
    }
)
_QUEUE_STATE_KEYS = frozenset(
    {
        "schema",
        "state",
        "tail_sequence",
        "tail_entry_digest",
        "previous_tail_sequence",
        "previous_tail_entry_digest",
        "pending_entry",
        "grants_authority",
        "state_digest",
    }
)
_DRAIN_CURSOR_KEYS = frozenset(
    {
        "schema",
        "queue_sequence",
        "entry_digest",
        "previous_entry_digest",
        "previous_cursor_digest",
        "queue_ref_digest",
        "delivery_ref_digest",
        "terminal_disposition",
        "ack_digest",
        "canonical_event_digest",
        "continuity_binding_digest",
        "attempt_state_digest",
        "attempt_ref",
        "attempt_digest",
        "acknowledged_count",
        "manual_review_count",
        "grants_authority",
        "cursor_digest",
    }
)
_DRAIN_CURSOR_POINTER_KEYS = frozenset(
    {
        "schema",
        "queue_sequence",
        "entry_digest",
        "cursor_digest",
        "acknowledged_count",
        "manual_review_count",
        "grants_authority",
        "pointer_digest",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {
        "schema",
        "client_id",
        "session_id",
        "state",
        "sequence",
        "entry_digest",
        "previous_sequence",
        "previous_entry_digest",
        "delivery_id",
        "pending_entry",
        "grants_authority",
        "checkpoint_digest",
    }
)
_INSTANCE_KEYS = frozenset(
    {
        "schema",
        "client_id",
        "client_instance_id",
        "grants_authority",
        "instance_digest",
    }
)
_ATTEMPT_KEYS = frozenset(
    {
        "schema",
        "entry_digest",
        "outcome",
        "evidence_digest",
        "retry_key",
        "attempt_number",
        "failure_count",
        "disposition",
        "terminal_reason",
        "predecessor_entry_digest",
        "predecessor_attempt_state_digest",
        "attempted_at",
        "grants_authority",
        "attempt_digest",
    }
)
_ATTEMPT_STATE_KEYS = frozenset(
    {
        "schema",
        "entry_digest",
        "attempt_count",
        "failure_count",
        "latest_attempt_digest",
        "latest_attempt_ref",
        "latest_retry_key",
        "disposition",
        "terminal_reason",
        "predecessor_entry_digest",
        "predecessor_attempt_state_digest",
        "grants_authority",
        "state_digest",
    }
)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _timestamp(value: dt.datetime, *, label: str) -> str:
    if value.tzinfo is None:
        raise ValidationFailed(f"{label} timezone-aware olmali")
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, *, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ValidationFailed(f"{label} timestamp olmali")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailed(f"{label} timestamp gecersiz") from exc
    if parsed.tzinfo is None:
        raise ValidationFailed(f"{label} timezone-aware olmali")
    return parsed


def _safe_text(value: Any, *, label: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ValidationFailed(f"{label} bounded metin olmali")
    return value


def _digest_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{label} digest metni olmali")
    parse_digest(value)
    return value


def _validate_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    observation = dict(value)
    if frozenset(observation) != _OBSERVATION_KEYS:
        raise ValidationFailed("Lifecycle observation exact content-free schema ister")
    for key in _FALSE_KEYS:
        if observation.get(key) is not False:
            raise PolicyViolation("Lifecycle observation content veya authority tasiyamaz")
    for key in (
        "schema",
        "client_id",
        "client_kind",
        "client_version",
        "session_id",
        "external_event_type",
        "internal_event_type",
    ):
        _safe_text(observation.get(key), label=key)
    for key in ("turn_id", "source", "trigger", "reason", "permission_mode"):
        _safe_text(observation.get(key), label=key, optional=True)
    if not isinstance(observation.get("stop_hook_active"), bool):
        raise ValidationFailed("stop_hook_active boolean olmali")
    wire_digest = observation.get("wire_digest")
    if not isinstance(wire_digest, str):
        raise ValidationFailed("wire_digest metin olmali")
    parse_digest(wire_digest)
    return observation


@dataclass(frozen=True, slots=True)
class LifecycleSpoolEntry:
    entry_digest: str
    delivery_id: str
    client_id: str
    client_kind: str
    client_version: str
    session_id: str
    sequence: int
    previous_entry_digest: str | None
    external_event_type: str
    internal_event_type: str
    observation_digest: str
    observation: dict[str, Any]
    occurred_at: dt.datetime

    def body(self) -> dict[str, Any]:
        return {
            "schema": SPOOL_ENTRY_SCHEMA,
            "delivery_id": self.delivery_id,
            "client_id": self.client_id,
            "client_kind": self.client_kind,
            "client_version": self.client_version,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "previous_entry_digest": self.previous_entry_digest,
            "external_event_type": self.external_event_type,
            "internal_event_type": self.internal_event_type,
            "observation_digest": self.observation_digest,
            "observation": self.observation,
            "occurred_at": _timestamp(self.occurred_at, label="occurred_at"),
            "grants_authority": False,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"entry_digest": self.entry_digest}

    def assert_integrity(self) -> None:
        parse_digest(self.entry_digest)
        parse_digest(self.delivery_id)
        if self.sequence < 1 or (self.sequence == 1) is not (
            self.previous_entry_digest is None
        ):
            raise PolicyViolation("Lifecycle spool sequence zinciri gecersiz")
        if self.previous_entry_digest is not None:
            parse_digest(self.previous_entry_digest)
        if self.observation_digest != digest(self.observation):
            raise PolicyViolation("Lifecycle spool observation digest mismatch")
        if self.entry_digest != digest(self.body()):
            raise PolicyViolation("Lifecycle spool entry digest mismatch")
        checked = _validate_observation(self.observation)
        if (
            checked["client_id"] != self.client_id
            or checked["client_kind"] != self.client_kind
            or checked["client_version"] != self.client_version
            or checked["session_id"] != self.session_id
            or checked["external_event_type"] != self.external_event_type
            or checked["internal_event_type"] != self.internal_event_type
        ):
            raise PolicyViolation("Lifecycle spool envelope/observation binding mismatch")


@dataclass(frozen=True, slots=True)
class LifecycleReplayResult:
    entry_digest: str
    outcome: str
    canonical_ack_digest: str | None
    attempt_digest: str
    grants_authority: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_digest": self.entry_digest,
            "outcome": self.outcome,
            "canonical_ack_digest": self.canonical_ack_digest,
            "attempt_digest": self.attempt_digest,
            "grants_authority": False,
        }


class LifecycleAckLike(Protocol):
    event_id: UUID
    local_event_digest: str
    canonical_digest: str


class LifecycleContinuityAdmission(Protocol):
    """Governed preflight, transactional apply and read-only lookup adapter.

    Implementations must own project/work/run resolution, authorization,
    claim-before-effect and terminal receipt lookup. The local spool never
    invents those identities.
    """

    def preflight(
        self,
        entry: LifecycleSpoolEntry,
        canonical_event: Mapping[str, Any],
        *,
        client_instance_id: str,
    ) -> Mapping[str, Any]: ...

    def apply(
        self,
        entry: LifecycleSpoolEntry,
        canonical_event: Mapping[str, Any],
        *,
        preflight: Mapping[str, Any],
        client_instance_id: str,
        now: dt.datetime,
    ) -> CanonicalLifecycleReceipt: ...

    def lookup(
        self,
        entry: LifecycleSpoolEntry,
        canonical_event: Mapping[str, Any],
        *,
        preflight: Mapping[str, Any],
        client_instance_id: str,
    ) -> CanonicalLifecycleReceipt: ...


@dataclass(frozen=True, slots=True)
class CanonicalLifecycleReceipt:
    """Exact PostgreSQL ingest plus idempotent lookup binding for one spool entry."""

    entry_digest: str
    canonical_event_digest: str
    canonical_event_id: UUID
    canonical_ack_digest: str
    canonical_lookup_digest: str
    runtime_binding_id: UUID | None
    runtime_binding_digest: str | None
    continuity_binding: dict[str, Any] | None
    grants_authority: bool = False

    @classmethod
    def verified(
        cls,
        entry: LifecycleSpoolEntry,
        canonical_event: Mapping[str, Any],
        first: LifecycleAckLike,
        lookup: LifecycleAckLike,
    ) -> CanonicalLifecycleReceipt:
        event_digest = _digest_text(
            canonical_event.get("event_digest"), label="canonical event_digest"
        )
        body = {key: value for key, value in canonical_event.items() if key != "event_digest"}
        if event_digest != digest(body):
            raise PolicyViolation("Canonical lifecycle event digest mismatch")
        for candidate in (first, lookup):
            if candidate.local_event_digest != event_digest:
                raise PolicyViolation("Canonical lifecycle receipt event binding mismatch")
            parse_digest(candidate.canonical_digest)
            if not isinstance(candidate.event_id, UUID):
                raise ValidationFailed("Canonical lifecycle receipt event_id UUID olmali")
        if (
            first.event_id != lookup.event_id
            or first.canonical_digest != lookup.canonical_digest
        ):
            raise PolicyViolation("Canonical lifecycle terminal receipt lookup drift")
        first_binding_id = getattr(first, "compaction_outbox_id", None)
        lookup_binding_id = getattr(lookup, "compaction_outbox_id", None)
        first_binding_digest = getattr(first, "compaction_payload_digest", None)
        lookup_binding_digest = getattr(lookup, "compaction_payload_digest", None)
        if (
            first_binding_id != lookup_binding_id
            or first_binding_digest != lookup_binding_digest
        ):
            raise PolicyViolation("Canonical lifecycle runtime binding lookup drift")
        if entry.internal_event_type == "pre_compaction":
            if not isinstance(lookup_binding_id, UUID) or not isinstance(
                lookup_binding_digest, str
            ):
                raise PolicyViolation(
                    "Pre-compaction canonical ACK exact runtime binding outbox ister"
                )
            parse_digest(lookup_binding_digest)
        elif lookup_binding_id is not None or lookup_binding_digest is not None:
            raise PolicyViolation("Non-compaction lifecycle unexpected runtime binding tasiyor")
        lookup_digest = digest(
            {
                "schema": "zekam-client-lifecycle-canonical-lookup/v1",
                "entry_digest": entry.entry_digest,
                "event_digest": event_digest,
                "event_id": str(lookup.event_id),
                "canonical_ack_digest": lookup.canonical_digest,
                "runtime_binding_id": (
                    None if lookup_binding_id is None else str(lookup_binding_id)
                ),
                "runtime_binding_digest": lookup_binding_digest,
                "lookup_verified": True,
                "grants_authority": False,
            }
        )
        return cls(
            entry.entry_digest,
            event_digest,
            lookup.event_id,
            lookup.canonical_digest,
            lookup_digest,
            lookup_binding_id,
            lookup_binding_digest,
            None,
            False,
        )

    def bind_continuity(
        self,
        entry: LifecycleSpoolEntry,
        document: Mapping[str, Any],
    ) -> CanonicalLifecycleReceipt:
        """Bind the terminal continuity receipt produced by governed admission."""

        checked = _validate_continuity_binding(
            document,
            entry=entry,
            canonical_event_digest=self.canonical_event_digest,
        )
        if self.continuity_binding is not None and self.continuity_binding != checked:
            raise PolicyViolation("Lifecycle continuity receipt replay drift")
        return replace(self, continuity_binding=checked)

    def assert_binding(self, entry: LifecycleSpoolEntry) -> None:
        for value in (
            self.entry_digest,
            self.canonical_event_digest,
            self.canonical_ack_digest,
            self.canonical_lookup_digest,
        ):
            parse_digest(value)
        if self.runtime_binding_digest is not None:
            parse_digest(self.runtime_binding_digest)
        if self.entry_digest != entry.entry_digest or self.grants_authority:
            raise PolicyViolation("Canonical lifecycle receipt spool binding mismatch")
        if entry.internal_event_type == "pre_compaction" and (
            self.runtime_binding_id is None or self.runtime_binding_digest is None
        ):
            raise PolicyViolation("Pre-compaction receipt runtime binding eksik")
        if self.continuity_binding is None:
            raise PolicyViolation("Lifecycle ACK terminal continuity binding ister")
        _validate_continuity_binding(
            self.continuity_binding,
            entry=entry,
            canonical_event_digest=self.canonical_event_digest,
        )


def _validate_continuity_binding(
    value: Mapping[str, Any],
    *,
    entry: LifecycleSpoolEntry,
    canonical_event_digest: str,
) -> dict[str, Any]:
    document = dict(value)
    if (
        frozenset(document) != _CONTINUITY_BINDING_KEYS
        or document.get("schema") != CONTINUITY_BINDING_SCHEMA
        or document.get("entry_digest") != entry.entry_digest
        or document.get("canonical_event_digest") != canonical_event_digest
        or document.get("event_type") != entry.internal_event_type
        or document.get("session_id") != entry.session_id
        or document.get("client_id") != entry.client_id
        or document.get("status") != "completed"
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle continuity binding schema/binding gecersiz")
    for key in (
        "entry_digest",
        "canonical_event_digest",
        "continuity_event_digest",
        "plan_digest",
        "effect_digest",
        "effect_receipt_digest",
        "terminal_receipt_digest",
        "binding_digest",
    ):
        _digest_text(document.get(key), label=f"continuity binding {key}")
    for key in (
        "realm_id",
        "project_id",
        "work_item_id",
        "run_id",
        "authorization_id",
        "job_id",
        "claim_id",
        "effect_receipt_id",
        "continuity_event_id",
        "delivery_outbox_id",
    ):
        raw = document.get(key)
        if not isinstance(raw, str):
            raise ValidationFailed(f"continuity binding {key} UUID olmali")
        try:
            if str(UUID(raw)) != raw:
                raise ValueError
        except ValueError as exc:
            raise ValidationFailed(f"continuity binding {key} UUID olmali") from exc
    for key in ("event_type", "session_id", "client_id", "status"):
        _safe_text(document.get(key), label=f"continuity binding {key}")
    compiler_enqueue = document.get("compiler_enqueue")
    if not isinstance(compiler_enqueue, bool):
        raise ValidationFailed("continuity binding compiler_enqueue boolean olmali")
    if entry.internal_event_type == "pre_compaction" and not compiler_enqueue:
        raise PolicyViolation("Pre-compaction continuity compiler enqueue ister")
    body = {key: item for key, item in document.items() if key != "binding_digest"}
    if digest(body) != document["binding_digest"]:
        raise PolicyViolation("Lifecycle continuity binding digest mismatch")
    return document


def _validate_continuity_preflight(
    value: Mapping[str, Any],
    *,
    entry: LifecycleSpoolEntry,
    canonical_event_digest: str,
    client_instance_id: str,
) -> dict[str, Any]:
    document = dict(value)
    if (
        frozenset(document) != _CONTINUITY_PREFLIGHT_KEYS
        or document.get("schema") != CONTINUITY_PREFLIGHT_SCHEMA
        or document.get("entry_digest") != entry.entry_digest
        or document.get("canonical_event_digest") != canonical_event_digest
        or document.get("client_instance_id") != client_instance_id
        or document.get("allowed") is not True
        or document.get("mutation_performed") is not False
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle continuity preflight schema/binding gecersiz")
    for key in (
        "entry_digest",
        "canonical_event_digest",
        "plan_digest",
        "effect_digest",
        "preflight_digest",
    ):
        _digest_text(document.get(key), label=f"continuity preflight {key}")
    for key in (
        "realm_id",
        "project_id",
        "work_item_id",
        "run_id",
        "authorization_id",
        "job_id",
        "claim_id",
    ):
        raw = document.get(key)
        if not isinstance(raw, str):
            raise ValidationFailed(f"continuity preflight {key} UUID olmali")
        try:
            if str(UUID(raw)) != raw:
                raise ValueError
        except ValueError as exc:
            raise ValidationFailed(f"continuity preflight {key} UUID olmali") from exc
    if _CLIENT_INSTANCE.fullmatch(client_instance_id) is None:
        raise ValidationFailed("continuity preflight client instance gecersiz")
    body = {key: item for key, item in document.items() if key != "preflight_digest"}
    if digest(body) != document["preflight_digest"]:
        raise PolicyViolation("Lifecycle continuity preflight digest mismatch")
    return document


def _assert_preflight_receipt_binding(
    preflight: Mapping[str, Any],
    receipt: CanonicalLifecycleReceipt,
) -> None:
    binding = receipt.continuity_binding
    if binding is None:
        raise PolicyViolation("Lifecycle continuity receipt binding eksik")
    for key in (
        "realm_id",
        "project_id",
        "work_item_id",
        "run_id",
        "authorization_id",
        "job_id",
        "claim_id",
        "plan_digest",
        "effect_digest",
    ):
        if binding.get(key) != preflight.get(key):
            raise PolicyViolation("Lifecycle preflight/terminal receipt binding drift")


def _entry_from_document(document: Any) -> LifecycleSpoolEntry:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _ENTRY_KEYS
        or document.get("schema") != SPOOL_ENTRY_SCHEMA
    ):
        raise ValidationFailed("Lifecycle spool entry schema gecersiz")
    if document.get("grants_authority") is not False:
        raise PolicyViolation("Lifecycle spool entry authority veremez")
    observation = document.get("observation")
    if not isinstance(observation, dict):
        raise ValidationFailed("Lifecycle spool observation object olmali")
    entry = LifecycleSpoolEntry(
        entry_digest=_digest_text(document.get("entry_digest"), label="entry_digest"),
        delivery_id=_digest_text(document.get("delivery_id"), label="delivery_id"),
        client_id=_safe_text(document.get("client_id"), label="client_id") or "",
        client_kind=_safe_text(document.get("client_kind"), label="client_kind") or "",
        client_version=_safe_text(document.get("client_version"), label="client_version") or "",
        session_id=_safe_text(document.get("session_id"), label="session_id") or "",
        sequence=document.get("sequence"),
        previous_entry_digest=(
            None
            if document.get("previous_entry_digest") is None
            else _digest_text(
                document.get("previous_entry_digest"), label="previous_entry_digest"
            )
        ),
        external_event_type=(
            _safe_text(document.get("external_event_type"), label="external_event_type") or ""
        ),
        internal_event_type=(
            _safe_text(document.get("internal_event_type"), label="internal_event_type") or ""
        ),
        observation_digest=_digest_text(
            document.get("observation_digest"), label="observation_digest"
        ),
        observation=dict(observation),
        occurred_at=_parse_timestamp(document.get("occurred_at"), label="occurred_at"),
    )
    if not isinstance(entry.sequence, int) or isinstance(entry.sequence, bool):
        raise ValidationFailed("Lifecycle spool sequence integer olmali")
    entry.assert_integrity()
    return entry


class ClientLifecycleSpool:
    """Append-only, per-home lifecycle outbox with a verified hash chain."""

    def __init__(self, home: Path, *, client_id: str) -> None:
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
        if not client_id or any(char not in allowed for char in client_id):
            raise ValidationFailed("Lifecycle spool client_id canonical olmali")
        self.root = home / "global" / "runtime" / "client-lifecycle" / client_id
        self.events_directory = self.root / "events"
        self.acks_directory = self.root / "acks"
        self.attempts_directory = self.root / "attempts"
        self.attempt_states_directory = self.root / "attempt-states"
        self.deliveries_directory = self.root / "deliveries"
        self.sessions_directory = self.root / "sessions"
        self.queue_directory = self.root / "queue"
        self.drain_cursors_directory = self.root / "drain-cursors"
        self.queue_state_path = self.root / "queue-state.json"
        self.drain_cursor_path = self.root / "drain-cursor.json"
        self.instance_path = self.root / "client-instance.json"
        self.lock_path = self.root / "writer.lock"

    def stage(
        self,
        observation: Mapping[str, Any],
        *,
        delivery_id: str,
        occurred_at: dt.datetime | None = None,
    ) -> LifecycleSpoolEntry:
        """Append once; an identical delivery is an idempotent replay."""

        parse_digest(delivery_id)
        safe = _validate_observation(observation)
        at = occurred_at or _utc_now()
        _timestamp(at, label="occurred_at")
        self._ensure_write_directories()
        with _exclusive_lock(self.lock_path):
            queue_sequence, queue_previous = self._load_queue_tail(recover=True)
            previous = self._load_session_tail(
                client_id=str(safe["client_id"]), session_id=str(safe["session_id"])
            )
            existing = self._entry_for_delivery(delivery_id)
            if existing is not None:
                if existing.observation_digest != digest(safe) or existing.observation != safe:
                    raise PolicyViolation("Lifecycle delivery replay payload drift")
                return existing
            draft = LifecycleSpoolEntry(
                entry_digest="",
                delivery_id=delivery_id,
                client_id=safe["client_id"],
                client_kind=safe["client_kind"],
                client_version=safe["client_version"],
                session_id=safe["session_id"],
                sequence=1 if previous is None else previous.sequence + 1,
                previous_entry_digest=None if previous is None else previous.entry_digest,
                external_event_type=safe["external_event_type"],
                internal_event_type=safe["internal_event_type"],
                observation_digest=digest(safe),
                observation=safe,
                occurred_at=at,
            )
            entry = replace(draft, entry_digest=digest(draft.body()))
            entry.assert_integrity()
            next_queue_sequence = queue_sequence + 1
            _write_atomic_json(
                self.queue_state_path,
                self._queue_state_document(
                    entry,
                    queue_sequence=next_queue_sequence,
                    previous_queue_entry_digest=queue_previous,
                    state="pending",
                ),
            )
            pending = self._checkpoint_document(entry, state="pending")
            _write_atomic_json(self._session_path(entry.client_id, entry.session_id), pending)
            _write_immutable_json(self._entry_path(entry.entry_digest), entry.as_dict())
            _write_immutable_json(
                self._delivery_path(entry.delivery_id),
                self._delivery_document(entry, queue_sequence=next_queue_sequence),
            )
            _write_immutable_json(
                self._queue_path(next_queue_sequence),
                self._queue_ref_document(
                    entry,
                    queue_sequence=next_queue_sequence,
                    previous_queue_entry_digest=queue_previous,
                ),
            )
            _write_atomic_json(
                self._session_path(entry.client_id, entry.session_id),
                self._checkpoint_document(entry, state="committed"),
            )
            _write_atomic_json(
                self.queue_state_path,
                self._queue_state_document(
                    entry,
                    queue_sequence=next_queue_sequence,
                    previous_queue_entry_digest=queue_previous,
                    state="committed",
                ),
            )
            return entry

    def client_instance_id(self) -> str:
        """Return one persistent, path-free Codex instance identity for canonical ingest."""

        self._ensure_write_directories()
        with _exclusive_lock(self.lock_path):
            if _safe_regular_file_exists(self.instance_path):
                return _validate_instance(_read_json(self.instance_path), self.root.name)
            instance_id = f"{self.root.name}-{uuid4()}"
            body = {
                "schema": SPOOL_INSTANCE_SCHEMA,
                "client_id": self.root.name,
                "client_instance_id": instance_id,
                "grants_authority": False,
            }
            document = body | {"instance_digest": digest(body)}
            _write_immutable_json(self.instance_path, document)
            return instance_id

    def pending(
        self,
        *,
        limit: int = 100,
    ) -> tuple[LifecycleSpoolEntry, ...]:
        if limit < 1 or limit > MAX_PENDING_BATCH:
            raise ValidationFailed(f"Lifecycle pending limit 1..{MAX_PENDING_BATCH} olmali")
        # An immutable ACK may survive a crash immediately before its derived
        # cursor replace. Repair at most one bounded page before selection so
        # the queue cannot remain permanently parked behind an already ACKed
        # entry. This never scans event or ACK history.
        if _safe_regular_file_exists(self.queue_state_path):
            with _exclusive_lock(self.lock_path):
                self._advance_drain_cursor()
        tail_sequence, _ = self._load_queue_tail(recover=False)
        cursor_sequence, _, _, _, _ = self._read_drain_cursor()
        if cursor_sequence > tail_sequence:
            raise PolicyViolation("Lifecycle drain cursor queue tail ilerisinde")
        effective_after = cursor_sequence
        start = effective_after + 1
        previous_digest: str | None = None
        if start > 1 and start <= tail_sequence:
            previous_ref = _read_json(self._queue_path(start - 1))
            _validate_queue_ref(
                previous_ref,
                queue_sequence=start - 1,
                previous_queue_entry_digest=(
                    None
                    if start == 2
                    else str(
                        _read_json(self._queue_path(start - 2))["entry_digest"]
                    )
                ),
            )
            previous_digest = str(previous_ref["entry_digest"])
        selected: list[LifecycleSpoolEntry] = []
        upper = min(tail_sequence, effective_after + limit)
        for sequence in range(start, upper + 1):
            queue_ref = _read_json(self._queue_path(sequence))
            _validate_queue_ref(
                queue_ref,
                queue_sequence=sequence,
                previous_queue_entry_digest=previous_digest,
            )
            entry_digest = str(queue_ref["entry_digest"])
            entry = self._read_entry(entry_digest)
            if entry is None:
                raise PolicyViolation("Lifecycle queue source entry eksik")
            delivery = _read_json(self._delivery_path(entry.delivery_id))
            _validate_delivery_ref(delivery, delivery_id=entry.delivery_id)
            if (
                delivery["entry_digest"] != entry.entry_digest
                or delivery["queue_sequence"] != sequence
            ):
                raise PolicyViolation("Lifecycle queue/delivery binding mismatch")
            attempt_state = self._read_attempt_state(entry.entry_digest)
            if (
                not _safe_regular_file_exists(self._ack_path(entry.entry_digest))
                and (
                    attempt_state is None
                    or attempt_state["disposition"] != "manual-review"
                )
            ):
                selected.append(entry)
            previous_digest = entry.entry_digest
        return tuple(selected)

    def _acknowledge_verified_receipt(
        self,
        entry: LifecycleSpoolEntry,
        *,
        receipt: CanonicalLifecycleReceipt,
        acknowledged_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Add a local ACK only after exact canonical ingest and lookup agree."""

        entry.assert_integrity()
        receipt.assert_binding(entry)
        if not _safe_regular_file_exists(self.instance_path):
            raise PolicyViolation("Lifecycle ACK canonical client instance ister")
        instance_id = _validate_instance(_read_json(self.instance_path), self.root.name)
        expected_event = canonical_lifecycle_event(
            entry,
            client_instance_id=instance_id,
            previous_canonical_event_digest=self.previous_canonical_event_digest(entry),
        )
        if expected_event["event_digest"] != receipt.canonical_event_digest:
            raise PolicyViolation("Lifecycle ACK canonical event/spool binding mismatch")
        at = acknowledged_at or _utc_now()
        _timestamp(at, label="acknowledged_at")
        self._ensure_write_directories()
        with _exclusive_lock(self.lock_path):
            persisted = self._read_entry(entry.entry_digest)
            if persisted != entry:
                raise ValidationFailed("Lifecycle ACK source entry bulunamadi")
            state = self._read_attempt_state(entry.entry_digest)
            if state is None or state["disposition"] != "completed":
                raise PolicyViolation(
                    "Lifecycle ACK exact completed attempt-state ister"
                )
            attempt = _read_json(
                self._attempt_path(str(state["latest_attempt_ref"]))
            )
            if attempt["evidence_digest"] != receipt.canonical_lookup_digest:
                raise PolicyViolation(
                    "Lifecycle ACK completed attempt/lookup evidence mismatch"
                )
            path = self._ack_path(entry.entry_digest)
            if _safe_regular_file_exists(path):
                existing = _read_json(path)
                _validate_ack(existing, entry_digest=entry.entry_digest)
                if (
                    existing["canonical_event_digest"] != receipt.canonical_event_digest
                    or existing["canonical_event_id"] != str(receipt.canonical_event_id)
                    or existing["canonical_ack_digest"] != receipt.canonical_ack_digest
                    or existing["canonical_lookup_digest"]
                    != receipt.canonical_lookup_digest
                    or existing["runtime_binding_id"]
                    != (
                        None
                        if receipt.runtime_binding_id is None
                        else str(receipt.runtime_binding_id)
                    )
                    or existing["runtime_binding_digest"]
                    != receipt.runtime_binding_digest
                    or existing["continuity_binding"] != receipt.continuity_binding
                ):
                    raise PolicyViolation("Lifecycle ACK replay digest drift")
                self._advance_drain_cursor()
                return existing
            body = {
                "schema": SPOOL_ACK_SCHEMA,
                "entry_digest": entry.entry_digest,
                "canonical_event_digest": receipt.canonical_event_digest,
                "canonical_event_id": str(receipt.canonical_event_id),
                "canonical_ack_digest": receipt.canonical_ack_digest,
                "canonical_lookup_digest": receipt.canonical_lookup_digest,
                "runtime_binding_id": (
                    None
                    if receipt.runtime_binding_id is None
                    else str(receipt.runtime_binding_id)
                ),
                "runtime_binding_digest": receipt.runtime_binding_digest,
                "continuity_binding": receipt.continuity_binding,
                "acknowledged_at": _timestamp(at, label="acknowledged_at"),
                "grants_authority": False,
            }
            document = body | {"ack_digest": digest(body)}
            _write_immutable_json(path, document)
            self._advance_drain_cursor()
            return document

    def acknowledge_committed_receipt(
        self,
        entry: LifecycleSpoolEntry,
        *,
        receipt: CanonicalLifecycleReceipt,
        acknowledged_at: dt.datetime | None = None,
    ) -> LifecycleReplayResult:
        """Write only local attempt/ACK state for an already committed DB terminal chain."""

        at = acknowledged_at or _utc_now()
        attempt = self.record_attempt(
            entry.entry_digest,
            outcome="completed",
            evidence_digest=receipt.canonical_lookup_digest,
            attempted_at=at,
        )
        self._acknowledge_verified_receipt(entry, receipt=receipt, acknowledged_at=at)
        return LifecycleReplayResult(
            entry.entry_digest,
            "completed",
            receipt.canonical_ack_digest,
            str(attempt["attempt_digest"]),
        )

    def record_attempt(
        self,
        entry_digest: str,
        *,
        outcome: str,
        evidence_digest: str,
        attempted_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Record one replay attempt without treating it as an ACK."""

        parse_digest(entry_digest)
        parse_digest(evidence_digest)
        checked_outcome = _safe_text(outcome, label="outcome")
        if checked_outcome not in {"completed", "deferred", "failed", "rejected"}:
            raise ValidationFailed("Lifecycle replay outcome canonical degil")
        at = attempted_at or _utc_now()
        _timestamp(at, label="attempted_at")
        self._ensure_write_directories()
        with _exclusive_lock(self.lock_path):
            if self._read_entry(entry_digest) is None:
                raise ValidationFailed("Lifecycle attempt source entry bulunamadi")
            retry_key = digest(
                {
                    "entry_digest": entry_digest,
                    "outcome": checked_outcome,
                    "evidence_digest": evidence_digest,
                }
            )
            state = self._read_attempt_state(entry_digest)
            if state is not None and state["latest_retry_key"] == retry_key:
                return _read_json(
                    self._attempt_path(str(state["latest_attempt_ref"]))
                )
            if (
                state is not None
                and state["disposition"] in {"completed", "manual-review"}
            ):
                raise PolicyViolation(
                    "Lifecycle terminal attempt-state yeni attempt kabul etmez"
                )
            retry_path = self._attempt_path(retry_key)
            if _safe_regular_file_exists(retry_path):
                orphan = _read_json(retry_path)
                if (
                    not isinstance(orphan, dict)
                    or frozenset(orphan) != _ATTEMPT_KEYS
                    or orphan.get("entry_digest") != entry_digest
                    or orphan.get("outcome") != checked_outcome
                    or orphan.get("evidence_digest") != evidence_digest
                    or orphan.get("retry_key") != retry_key
                ):
                    raise PolicyViolation("Lifecycle orphan attempt retry drift")
                orphan_body = {
                    key: value
                    for key, value in orphan.items()
                    if key != "attempt_digest"
                }
                if digest(orphan_body) != orphan.get("attempt_digest"):
                    raise PolicyViolation("Lifecycle orphan attempt digest mismatch")
                expected_next = 1 if state is None else int(state["attempt_count"]) + 1
                if int(orphan["attempt_number"]) == expected_next:
                    recovered_state = {
                        "schema": SPOOL_ATTEMPT_STATE_SCHEMA,
                        "entry_digest": entry_digest,
                        "attempt_count": orphan["attempt_number"],
                        "failure_count": orphan["failure_count"],
                        "latest_attempt_digest": orphan["attempt_digest"],
                        "latest_attempt_ref": retry_key,
                        "latest_retry_key": retry_key,
                        "disposition": orphan["disposition"],
                        "terminal_reason": orphan["terminal_reason"],
                        "predecessor_entry_digest": orphan[
                            "predecessor_entry_digest"
                        ],
                        "predecessor_attempt_state_digest": orphan[
                            "predecessor_attempt_state_digest"
                        ],
                        "grants_authority": False,
                    }
                    _write_atomic_json(
                        self._attempt_state_path(entry_digest),
                        recovered_state | {"state_digest": digest(recovered_state)},
                    )
                elif state is None or int(orphan["attempt_number"]) > int(
                    state["attempt_count"]
                ):
                    raise PolicyViolation("Lifecycle orphan attempt sequence drift")
                return orphan
            attempt_count = 1 if state is None else int(state["attempt_count"]) + 1
            prior_failures = 0 if state is None else int(state["failure_count"])
            failure_count = prior_failures + int(checked_outcome != "completed")
            disposition = (
                "completed"
                if checked_outcome == "completed"
                else (
                    "manual-review"
                    if failure_count >= MAX_REPLAY_FAILURES
                    else "retryable"
                )
            )
            terminal_reason = (
                "retry-budget-exhausted"
                if disposition == "manual-review"
                else None
            )
            body = {
                "schema": SPOOL_ATTEMPT_SCHEMA,
                "entry_digest": entry_digest,
                "outcome": checked_outcome,
                "evidence_digest": evidence_digest,
                "retry_key": retry_key,
                "attempt_number": attempt_count,
                "failure_count": failure_count,
                "disposition": disposition,
                "terminal_reason": terminal_reason,
                "predecessor_entry_digest": None,
                "predecessor_attempt_state_digest": None,
                "attempted_at": _timestamp(at, label="attempted_at"),
                "grants_authority": False,
            }
            attempt_digest = digest(body)
            document = body | {"attempt_digest": attempt_digest}
            _write_immutable_json(retry_path, document)
            state_body = {
                "schema": SPOOL_ATTEMPT_STATE_SCHEMA,
                "entry_digest": entry_digest,
                "attempt_count": attempt_count,
                "failure_count": failure_count,
                "latest_attempt_digest": attempt_digest,
                "latest_attempt_ref": retry_key,
                "latest_retry_key": retry_key,
                "disposition": disposition,
                "terminal_reason": terminal_reason,
                "predecessor_entry_digest": None,
                "predecessor_attempt_state_digest": None,
                "grants_authority": False,
            }
            _write_atomic_json(
                self._attempt_state_path(entry_digest),
                state_body | {"state_digest": digest(state_body)},
            )
            return document

    def record_predecessor_manual_review(
        self,
        entry_digest: str,
        *,
        attempted_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Terminalize an entry whose exact session predecessor needs recovery."""

        parse_digest(entry_digest)
        at = attempted_at or _utc_now()
        _timestamp(at, label="attempted_at")
        self._ensure_write_directories()
        with _exclusive_lock(self.lock_path):
            entry = self._read_entry(entry_digest)
            if entry is None or entry.previous_entry_digest is None:
                raise PolicyViolation(
                    "Lifecycle predecessor manual-review exact ardil entry ister"
                )
            predecessor = self._read_entry(entry.previous_entry_digest)
            if (
                predecessor is None
                or predecessor.client_id != entry.client_id
                or predecessor.session_id != entry.session_id
                or predecessor.sequence + 1 != entry.sequence
                or _safe_regular_file_exists(self._ack_path(predecessor.entry_digest))
            ):
                raise PolicyViolation(
                    "Lifecycle predecessor manual-review chain binding mismatch"
                )
            predecessor_state = self._read_attempt_state(predecessor.entry_digest)
            if (
                predecessor_state is None
                or predecessor_state["disposition"] != "manual-review"
            ):
                raise PolicyViolation(
                    "Lifecycle predecessor terminal manual-review kaniti eksik"
                )
            terminal_reason = "predecessor-manual-review"
            evidence_digest = digest(
                {
                    "schema": "zekam-client-lifecycle-predecessor-block/v1",
                    "entry_digest": entry.entry_digest,
                    "predecessor_entry_digest": predecessor.entry_digest,
                    "predecessor_attempt_state_digest": predecessor_state[
                        "state_digest"
                    ],
                    "grants_authority": False,
                }
            )
            retry_key = digest(
                {
                    "entry_digest": entry.entry_digest,
                    "outcome": "rejected",
                    "evidence_digest": evidence_digest,
                    "terminal_reason": terminal_reason,
                }
            )
            state = self._read_attempt_state(entry.entry_digest)
            if state is not None and state["latest_retry_key"] == retry_key:
                return _read_json(
                    self._attempt_path(str(state["latest_attempt_ref"]))
                )
            if state is not None and state["disposition"] in {
                "completed",
                "manual-review",
            }:
                raise PolicyViolation(
                    "Lifecycle terminal attempt-state yeni dependency sonucu kabul etmez"
                )
            retry_path = self._attempt_path(retry_key)
            if _safe_regular_file_exists(retry_path):
                orphan = _read_json(retry_path)
                _validate_attempt(
                    orphan,
                    entry_digest=entry.entry_digest,
                    expected_retry_key=retry_key,
                )
                expected_next = 1 if state is None else int(state["attempt_count"]) + 1
                if (
                    orphan["attempt_number"] != expected_next
                    or orphan["terminal_reason"] != terminal_reason
                    or orphan["evidence_digest"] != evidence_digest
                ):
                    raise PolicyViolation(
                        "Lifecycle predecessor orphan attempt sequence drift"
                    )
                recovered_state = {
                    "schema": SPOOL_ATTEMPT_STATE_SCHEMA,
                    "entry_digest": entry.entry_digest,
                    "attempt_count": orphan["attempt_number"],
                    "failure_count": orphan["failure_count"],
                    "latest_attempt_digest": orphan["attempt_digest"],
                    "latest_attempt_ref": retry_key,
                    "latest_retry_key": retry_key,
                    "disposition": "manual-review",
                    "terminal_reason": terminal_reason,
                    "predecessor_entry_digest": predecessor.entry_digest,
                    "predecessor_attempt_state_digest": predecessor_state[
                        "state_digest"
                    ],
                    "grants_authority": False,
                }
                _write_atomic_json(
                    self._attempt_state_path(entry.entry_digest),
                    recovered_state | {"state_digest": digest(recovered_state)},
                )
                return orphan
            attempt_count = 1 if state is None else int(state["attempt_count"]) + 1
            failure_count = 1 if state is None else int(state["failure_count"]) + 1
            body = {
                "schema": SPOOL_ATTEMPT_SCHEMA,
                "entry_digest": entry.entry_digest,
                "outcome": "rejected",
                "evidence_digest": evidence_digest,
                "retry_key": retry_key,
                "attempt_number": attempt_count,
                "failure_count": failure_count,
                "disposition": "manual-review",
                "terminal_reason": terminal_reason,
                "predecessor_entry_digest": predecessor.entry_digest,
                "predecessor_attempt_state_digest": predecessor_state[
                    "state_digest"
                ],
                "attempted_at": _timestamp(at, label="attempted_at"),
                "grants_authority": False,
            }
            document = body | {"attempt_digest": digest(body)}
            _write_immutable_json(retry_path, document)
            state_body = {
                "schema": SPOOL_ATTEMPT_STATE_SCHEMA,
                "entry_digest": entry.entry_digest,
                "attempt_count": attempt_count,
                "failure_count": failure_count,
                "latest_attempt_digest": document["attempt_digest"],
                "latest_attempt_ref": retry_key,
                "latest_retry_key": retry_key,
                "disposition": "manual-review",
                "terminal_reason": terminal_reason,
                "predecessor_entry_digest": predecessor.entry_digest,
                "predecessor_attempt_state_digest": predecessor_state[
                    "state_digest"
                ],
                "grants_authority": False,
            }
            _write_atomic_json(
                self._attempt_state_path(entry.entry_digest),
                state_body | {"state_digest": digest(state_body)},
            )
            return document

    def status(
        self,
        *,
        limit: int = 100,
        after_sequence: int = 0,
    ) -> dict[str, Any]:
        """Return one bounded queue-index page; never scan full history."""

        if limit < 1 or limit > MAX_PENDING_BATCH or after_sequence < 0:
            raise ValidationFailed("Lifecycle status pagination gecersiz")
        tail_sequence, _ = self._load_queue_tail(recover=False)
        (
            cursor_sequence,
            _,
            _,
            acknowledged_prefix_count,
            manual_review_prefix_count,
        ) = self._read_drain_cursor()
        start = after_sequence + 1
        upper = min(tail_sequence, after_sequence + limit)
        pending_count = 0
        page_acked_count = 0
        attempt_count = 0
        manual_review_count = 0
        oldest_pending_at: str | None = None
        previous_digest: str | None = None
        if start > 1 and start <= tail_sequence:
            previous_ref = _read_json(self._queue_path(start - 1))
            previous_digest = _digest_text(
                previous_ref.get("entry_digest"), label="status previous entry"
            )
        for sequence in range(start, upper + 1):
            queue_ref = _read_json(self._queue_path(sequence))
            _validate_queue_ref(
                queue_ref,
                queue_sequence=sequence,
                previous_queue_entry_digest=previous_digest,
            )
            entry = self._read_entry(str(queue_ref["entry_digest"]))
            if entry is None:
                raise PolicyViolation("Lifecycle status queue entry eksik")
            delivery = _read_json(self._delivery_path(entry.delivery_id))
            _validate_delivery_ref(delivery, delivery_id=entry.delivery_id)
            if (
                delivery["entry_digest"] != entry.entry_digest
                or delivery["queue_sequence"] != sequence
            ):
                raise PolicyViolation("Lifecycle status delivery parity mismatch")
            state = self._read_attempt_state(entry.entry_digest)
            if state is not None:
                attempt_count += int(state["attempt_count"])
                manual_review_count += int(state["disposition"] == "manual-review")
            if _safe_regular_file_exists(self._ack_path(entry.entry_digest)):
                _validate_ack(
                    _read_json(self._ack_path(entry.entry_digest)),
                    entry_digest=entry.entry_digest,
                )
                page_acked_count += 1
            elif state is None or state["disposition"] != "manual-review":
                pending_count += 1
                if oldest_pending_at is None:
                    oldest_pending_at = _timestamp(
                        entry.occurred_at, label="oldest_pending_at"
                    )
            previous_digest = entry.entry_digest
        next_after = upper if upper < tail_sequence else None
        return {
            "schema": "zekam-client-lifecycle-spool-status/v3",
            "client_id": self.root.name,
            "event_count": tail_sequence,
            "resolved_count": cursor_sequence,
            "acked_count": acknowledged_prefix_count,
            "resolved_manual_review_count": manual_review_prefix_count,
            "page_attempt_count": attempt_count,
            "page_pending_count": pending_count,
            "page_manual_review_count": manual_review_count,
            "oldest_pending_at": oldest_pending_at,
            "page_after_sequence": after_sequence,
            "page_event_count": max(0, upper - start + 1),
            "page_acked_count": page_acked_count,
            "counts_scope": (
                "page-except-event-acked-and-manual-review-prefix"
            ),
            "next_after_sequence": next_after,
            "history_complete": next_after is None and after_sequence == 0,
            "grants_authority": False,
        }

    def _read_attempt_state(self, entry_digest: str) -> dict[str, Any] | None:
        path = self._attempt_state_path(entry_digest)
        if not _safe_regular_file_exists(path):
            return None
        document = _read_json(path)
        _validate_attempt_state(document, entry_digest=entry_digest)
        attempt = _read_json(
            self._attempt_path(str(document["latest_attempt_ref"]))
        )
        _validate_attempt(
            attempt,
            entry_digest=entry_digest,
            expected_retry_key=str(document["latest_attempt_ref"]),
        )
        if (
            attempt.get("attempt_digest") != document["latest_attempt_digest"]
            or attempt.get("retry_key") != document["latest_attempt_ref"]
            or attempt.get("retry_key") != document["latest_retry_key"]
            or attempt.get("attempt_number") != document["attempt_count"]
            or attempt.get("failure_count") != document["failure_count"]
            or attempt.get("disposition") != document["disposition"]
            or attempt.get("terminal_reason") != document["terminal_reason"]
            or attempt.get("predecessor_entry_digest")
            != document["predecessor_entry_digest"]
            or attempt.get("predecessor_attempt_state_digest")
            != document["predecessor_attempt_state_digest"]
        ):
            raise PolicyViolation("Lifecycle attempt state/latest receipt parity mismatch")
        return document

    def _verified_entries(self) -> list[LifecycleSpoolEntry]:
        if not _safe_directory_exists(self.events_directory):
            return []
        _assert_json_directory(self.events_directory, label="event")
        entries: list[LifecycleSpoolEntry] = []
        delivery_ids: set[str] = set()
        for path in _safe_json_files(self.events_directory):
            entry = _entry_from_document(_read_json(path))
            if path.stem != parse_digest(entry.entry_digest):
                raise PolicyViolation("Lifecycle spool filename digest mismatch")
            if entry.delivery_id in delivery_ids:
                raise PolicyViolation("Lifecycle spool duplicate delivery id")
            delivery_ids.add(entry.delivery_id)
            entries.append(entry)

        entries.sort(key=lambda item: (item.client_id, item.session_id, item.sequence))
        previous_by_session: dict[tuple[str, str], LifecycleSpoolEntry] = {}
        for item in entries:
            key = (item.client_id, item.session_id)
            previous = previous_by_session.get(key)
            expected_sequence = 1 if previous is None else previous.sequence + 1
            expected_digest = None if previous is None else previous.entry_digest
            if item.sequence != expected_sequence or item.previous_entry_digest != expected_digest:
                raise PolicyViolation("Lifecycle spool immutable chain kopuk")
            previous_by_session[key] = item
        entries.sort(key=lambda item: (item.client_id, item.session_id, item.sequence))
        return entries

    def _verified_ack_entry_digests(
        self, entries: list[LifecycleSpoolEntry]
    ) -> frozenset[str]:
        if not _safe_directory_exists(self.acks_directory):
            return frozenset()
        _assert_json_directory(self.acks_directory, label="ACK")
        existing = {item.entry_digest for item in entries}
        acked: set[str] = set()
        for path in _safe_json_files(self.acks_directory):
            document = _read_json(path)
            entry_digest = document.get("entry_digest") if isinstance(document, dict) else None
            if not isinstance(entry_digest, str):
                raise ValidationFailed("Lifecycle ACK entry digest eksik")
            _validate_ack(document, entry_digest=entry_digest)
            if path.stem != parse_digest(entry_digest) or entry_digest not in existing:
                raise PolicyViolation("Lifecycle ACK source/filename binding mismatch")
            if entry_digest in acked:
                raise PolicyViolation("Lifecycle duplicate ACK")
            acked.add(entry_digest)
        return frozenset(acked)

    def _verified_attempt_count(self, entries: list[LifecycleSpoolEntry]) -> int:
        if not _safe_directory_exists(self.attempts_directory):
            return 0
        _assert_json_directory(self.attempts_directory, label="attempt")
        existing = {item.entry_digest for item in entries}
        count = 0
        for path in _safe_json_files(self.attempts_directory):
            document = _read_json(path)
            if not isinstance(document, dict):
                raise ValidationFailed("Lifecycle attempt schema gecersiz")
            entry_digest = _digest_text(
                document.get("entry_digest"), label="attempt entry_digest"
            )
            _validate_attempt(
                document,
                entry_digest=entry_digest,
                expected_retry_key=str(document.get("retry_key")),
            )
            if (
                entry_digest not in existing
                or path.stem != parse_digest(str(document["retry_key"]))
            ):
                raise PolicyViolation("Lifecycle attempt source/digest binding mismatch")
            count += 1
        return count

    def previous_canonical_event_digest(self, entry: LifecycleSpoolEntry) -> str | None:
        """Resolve the already-ACKed canonical predecessor for ordered DB drain."""

        if entry.sequence == 1:
            return None
        assert entry.previous_entry_digest is not None
        path = self._ack_path(entry.previous_entry_digest)
        if not _safe_regular_file_exists(path):
            raise PolicyViolation("Lifecycle canonical predecessor receipt bekleniyor")
        document = _read_json(path)
        _validate_ack(document, entry_digest=entry.previous_entry_digest)
        return str(document["canonical_event_digest"])

    def _read_entry(self, entry_digest: str) -> LifecycleSpoolEntry | None:
        parse_digest(entry_digest)
        path = self._entry_path(entry_digest)
        if not _safe_regular_file_exists(path):
            return None
        entry = _entry_from_document(_read_json(path))
        if path.stem != parse_digest(entry.entry_digest):
            raise PolicyViolation("Lifecycle spool filename digest mismatch")
        return entry

    def _entry_for_delivery(self, delivery_id: str) -> LifecycleSpoolEntry | None:
        path = self._delivery_path(delivery_id)
        if not _safe_regular_file_exists(path):
            return None
        document = _read_json(path)
        _validate_delivery_ref(document, delivery_id=delivery_id)
        entry = self._read_entry(str(document["entry_digest"]))
        if entry is None:
            raise PolicyViolation("Lifecycle delivery ref source entry eksik")
        if (
            entry.delivery_id != delivery_id
            or entry.observation_digest != document["observation_digest"]
            or entry.client_id != document["client_id"]
            or entry.session_id != document["session_id"]
        ):
            raise PolicyViolation("Lifecycle delivery ref entry binding mismatch")
        return entry

    def _load_session_tail(
        self, *, client_id: str, session_id: str
    ) -> LifecycleSpoolEntry | None:
        path = self._session_path(client_id, session_id)
        if not _safe_regular_file_exists(path):
            return None
        checkpoint = _read_json(path)
        _validate_checkpoint(checkpoint, client_id=client_id, session_id=session_id)
        if checkpoint["state"] == "pending":
            raise PolicyViolation("Lifecycle session checkpoint global queue recovery ister")
        entry = self._read_entry(str(checkpoint["entry_digest"]))
        if entry is None:
            raise PolicyViolation("Lifecycle session checkpoint tail entry eksik")
        if (
            entry.client_id != client_id
            or entry.session_id != session_id
            or entry.sequence != checkpoint["sequence"]
            or entry.delivery_id != checkpoint["delivery_id"]
        ):
            raise PolicyViolation("Lifecycle session checkpoint tail binding mismatch")
        self._assert_bounded_tail(entry)
        return entry

    def _load_queue_tail(self, *, recover: bool) -> tuple[int, str | None]:
        if not _safe_regular_file_exists(self.queue_state_path):
            return 0, None
        state = _read_json(self.queue_state_path)
        _validate_queue_state(state)
        if state["state"] == "pending":
            if not recover:
                raise PolicyViolation("Lifecycle queue pending recovery gerekiyor")
            pending = state["pending_entry"]
            assert isinstance(pending, dict)
            entry = _entry_from_document(pending)
            queue_sequence = int(state["tail_sequence"])
            previous_queue_entry_digest = state["previous_tail_entry_digest"]
            _write_atomic_json(
                self._session_path(entry.client_id, entry.session_id),
                self._checkpoint_document(entry, state="pending"),
            )
            _write_immutable_json(self._entry_path(entry.entry_digest), entry.as_dict())
            _write_immutable_json(
                self._delivery_path(entry.delivery_id),
                self._delivery_document(entry, queue_sequence=queue_sequence),
            )
            _write_immutable_json(
                self._queue_path(queue_sequence),
                self._queue_ref_document(
                    entry,
                    queue_sequence=queue_sequence,
                    previous_queue_entry_digest=previous_queue_entry_digest,
                ),
            )
            self._assert_bounded_tail(entry)
            _write_atomic_json(
                self._session_path(entry.client_id, entry.session_id),
                self._checkpoint_document(entry, state="committed"),
            )
            state = self._queue_state_document(
                entry,
                queue_sequence=queue_sequence,
                previous_queue_entry_digest=previous_queue_entry_digest,
                state="committed",
            )
            _write_atomic_json(self.queue_state_path, state)
        queue_sequence = int(state["tail_sequence"])
        tail_digest = state["tail_entry_digest"]
        if queue_sequence > 0:
            queue_ref = _read_json(self._queue_path(queue_sequence))
            _validate_queue_ref(
                queue_ref,
                queue_sequence=queue_sequence,
                previous_queue_entry_digest=state["previous_tail_entry_digest"],
            )
            if queue_ref["entry_digest"] != tail_digest:
                raise PolicyViolation("Lifecycle queue state tail binding mismatch")
        return queue_sequence, None if tail_digest is None else str(tail_digest)

    def _read_drain_cursor(
        self,
    ) -> tuple[int, str | None, str | None, int, int]:
        if not _safe_regular_file_exists(self.drain_cursor_path):
            return 0, None, None, 0, 0
        pointer = _read_json(self.drain_cursor_path)
        _validate_drain_cursor_pointer(pointer)
        sequence = int(pointer["queue_sequence"])
        record = _read_json(self._drain_cursor_record_path(sequence))
        self._validate_drain_cursor_record(record, expected_sequence=sequence)
        if (
            pointer["entry_digest"] != record["entry_digest"]
            or pointer["cursor_digest"] != record["cursor_digest"]
            or pointer["acknowledged_count"] != record["acknowledged_count"]
            or pointer["manual_review_count"] != record["manual_review_count"]
        ):
            raise PolicyViolation("Lifecycle drain cursor pointer binding mismatch")
        return (
            sequence,
            str(record["entry_digest"]),
            str(record["cursor_digest"]),
            int(record["acknowledged_count"]),
            int(record["manual_review_count"]),
        )

    def _validate_drain_cursor_record(
        self,
        document: Mapping[str, Any],
        *,
        expected_sequence: int,
    ) -> None:
        _validate_drain_cursor_record(document, expected_sequence=expected_sequence)
        previous_entry_digest = document["previous_entry_digest"]
        previous_cursor_digest = document["previous_cursor_digest"]
        if expected_sequence == 1:
            if previous_entry_digest is not None or previous_cursor_digest is not None:
                raise PolicyViolation("Lifecycle first cursor previous binding tasiyamaz")
            previous_acknowledged_count = 0
            previous_manual_review_count = 0
        else:
            previous = _read_json(
                self._drain_cursor_record_path(expected_sequence - 1)
            )
            _validate_drain_cursor_record(
                previous, expected_sequence=expected_sequence - 1
            )
            if (
                previous["entry_digest"] != previous_entry_digest
                or previous["cursor_digest"] != previous_cursor_digest
            ):
                raise PolicyViolation("Lifecycle drain cursor immutable chain kopuk")
            previous_acknowledged_count = int(previous["acknowledged_count"])
            previous_manual_review_count = int(previous["manual_review_count"])
        queue_ref = _read_json(self._queue_path(expected_sequence))
        _validate_queue_ref(
            queue_ref,
            queue_sequence=expected_sequence,
            previous_queue_entry_digest=previous_entry_digest,
        )
        if (
            queue_ref["entry_digest"] != document["entry_digest"]
            or queue_ref["ref_digest"] != document["queue_ref_digest"]
        ):
            raise PolicyViolation("Lifecycle drain cursor queue parity mismatch")
        entry = self._read_entry(str(document["entry_digest"]))
        if entry is None:
            raise PolicyViolation("Lifecycle drain cursor entry eksik")
        delivery = _read_json(self._delivery_path(entry.delivery_id))
        _validate_delivery_ref(delivery, delivery_id=entry.delivery_id)
        if (
            delivery["entry_digest"] != entry.entry_digest
            or delivery["queue_sequence"] != expected_sequence
            or delivery["observation_digest"] != entry.observation_digest
            or delivery["ref_digest"] != document["delivery_ref_digest"]
        ):
            raise PolicyViolation("Lifecycle drain cursor delivery parity mismatch")
        disposition = str(document["terminal_disposition"])
        expected_acknowledged_count = previous_acknowledged_count + int(
            disposition == "acknowledged"
        )
        expected_manual_review_count = previous_manual_review_count + int(
            disposition == "manual-review"
        )
        if (
            document["acknowledged_count"] != expected_acknowledged_count
            or document["manual_review_count"] != expected_manual_review_count
        ):
            raise PolicyViolation("Lifecycle drain cursor terminal sayac zinciri kopuk")
        state = self._read_attempt_state(entry.entry_digest)
        if state is None:
            raise PolicyViolation("Lifecycle drain cursor exact attempt-state ister")
        attempt_ref = str(document["attempt_ref"])
        attempt = _read_json(self._attempt_path(attempt_ref))
        _validate_attempt(
            attempt,
            entry_digest=entry.entry_digest,
            expected_retry_key=attempt_ref,
        )
        if (
            state["state_digest"] != document["attempt_state_digest"]
            or state["latest_attempt_ref"] != attempt_ref
            or state["latest_attempt_digest"] != document["attempt_digest"]
            or attempt["attempt_digest"] != document["attempt_digest"]
            or state["disposition"] != attempt["disposition"]
        ):
            raise PolicyViolation(
                "Lifecycle drain cursor attempt-state/receipt parity mismatch"
            )
        if disposition == "acknowledged":
            if state["disposition"] != "completed":
                raise PolicyViolation(
                    "Lifecycle ACK cursor completed attempt-state ister"
                )
            ack = _read_json(self._ack_path(entry.entry_digest))
            _validate_ack(ack, entry_digest=entry.entry_digest)
            continuity = ack["continuity_binding"]
            _validate_continuity_binding(
                continuity,
                entry=entry,
                canonical_event_digest=str(ack["canonical_event_digest"]),
            )
            if (
                ack["ack_digest"] != document["ack_digest"]
                or ack["canonical_event_digest"]
                != document["canonical_event_digest"]
                or attempt["evidence_digest"] != ack["canonical_lookup_digest"]
                or continuity["binding_digest"]
                != document["continuity_binding_digest"]
            ):
                raise PolicyViolation(
                    "Lifecycle drain cursor ACK/continuity parity mismatch"
                )
            return
        if (
            state["disposition"] != "manual-review"
            or _safe_regular_file_exists(self._ack_path(entry.entry_digest))
        ):
            raise PolicyViolation(
                "Lifecycle drain cursor manual-review receipt parity mismatch"
            )
        if attempt["terminal_reason"] == "predecessor-manual-review":
            predecessor_digest = str(attempt["predecessor_entry_digest"])
            predecessor = self._read_entry(predecessor_digest)
            predecessor_state = self._read_attempt_state(predecessor_digest)
            if (
                entry.previous_entry_digest != predecessor_digest
                or predecessor is None
                or predecessor.client_id != entry.client_id
                or predecessor.session_id != entry.session_id
                or predecessor.sequence + 1 != entry.sequence
                or predecessor_state is None
                or predecessor_state["disposition"] != "manual-review"
                or predecessor_state["state_digest"]
                != attempt["predecessor_attempt_state_digest"]
                or _safe_regular_file_exists(self._ack_path(predecessor_digest))
            ):
                raise PolicyViolation(
                    "Lifecycle drain cursor predecessor terminal chain drift"
                )

    def _cursor_record_document(
        self,
        *,
        queue_sequence: int,
        previous_entry_digest: str | None,
        previous_cursor_digest: str | None,
        previous_acknowledged_count: int,
        previous_manual_review_count: int,
    ) -> dict[str, Any]:
        queue_ref = _read_json(self._queue_path(queue_sequence))
        _validate_queue_ref(
            queue_ref,
            queue_sequence=queue_sequence,
            previous_queue_entry_digest=previous_entry_digest,
        )
        entry = self._read_entry(str(queue_ref["entry_digest"]))
        if entry is None:
            raise PolicyViolation("Lifecycle cursor source entry eksik")
        delivery = _read_json(self._delivery_path(entry.delivery_id))
        _validate_delivery_ref(delivery, delivery_id=entry.delivery_id)
        if (
            delivery["entry_digest"] != entry.entry_digest
            or delivery["queue_sequence"] != queue_sequence
            or delivery["observation_digest"] != entry.observation_digest
        ):
            raise PolicyViolation("Lifecycle cursor delivery/entry parity mismatch")
        state = self._read_attempt_state(entry.entry_digest)
        if state is None:
            raise PolicyViolation("Lifecycle cursor exact attempt-state ister")
        attempt_ref = str(state["latest_attempt_ref"])
        attempt_digest = str(state["latest_attempt_digest"])
        attempt_state_digest = str(state["state_digest"])
        ack_digest: str | None = None
        canonical_event_digest: str | None = None
        continuity_binding_digest: str | None = None
        if _safe_regular_file_exists(self._ack_path(entry.entry_digest)):
            if state["disposition"] != "completed":
                raise PolicyViolation(
                    "Lifecycle ACK cursor completed attempt-state ister"
                )
            terminal_disposition = "acknowledged"
            ack = _read_json(self._ack_path(entry.entry_digest))
            _validate_ack(ack, entry_digest=entry.entry_digest)
            continuity = ack["continuity_binding"]
            _validate_continuity_binding(
                continuity,
                entry=entry,
                canonical_event_digest=str(ack["canonical_event_digest"]),
            )
            ack_digest = str(ack["ack_digest"])
            canonical_event_digest = str(ack["canonical_event_digest"])
            continuity_binding_digest = str(continuity["binding_digest"])
        else:
            if state["disposition"] != "manual-review":
                raise PolicyViolation(
                    "Lifecycle cursor ACK veya terminal manual-review ister"
                )
            terminal_disposition = "manual-review"
        body = {
            "schema": SPOOL_DRAIN_CURSOR_SCHEMA,
            "queue_sequence": queue_sequence,
            "entry_digest": entry.entry_digest,
            "previous_entry_digest": previous_entry_digest,
            "previous_cursor_digest": previous_cursor_digest,
            "queue_ref_digest": queue_ref["ref_digest"],
            "delivery_ref_digest": delivery["ref_digest"],
            "terminal_disposition": terminal_disposition,
            "ack_digest": ack_digest,
            "canonical_event_digest": canonical_event_digest,
            "continuity_binding_digest": continuity_binding_digest,
            "attempt_state_digest": attempt_state_digest,
            "attempt_ref": attempt_ref,
            "attempt_digest": attempt_digest,
            "acknowledged_count": previous_acknowledged_count
            + int(terminal_disposition == "acknowledged"),
            "manual_review_count": previous_manual_review_count
            + int(terminal_disposition == "manual-review"),
            "grants_authority": False,
        }
        return body | {"cursor_digest": digest(body)}

    def _write_drain_cursor_pointer(self, record: Mapping[str, Any]) -> None:
        body = {
            "schema": SPOOL_DRAIN_CURSOR_POINTER_SCHEMA,
            "queue_sequence": record["queue_sequence"],
            "entry_digest": record["entry_digest"],
            "cursor_digest": record["cursor_digest"],
            "acknowledged_count": record["acknowledged_count"],
            "manual_review_count": record["manual_review_count"],
            "grants_authority": False,
        }
        _write_atomic_json(
            self.drain_cursor_path,
            body | {"pointer_digest": digest(body)},
        )

    def _advance_drain_cursor(self) -> None:
        (
            sequence,
            previous_digest,
            previous_cursor_digest,
            acknowledged_count,
            manual_review_count,
        ) = self._read_drain_cursor()
        tail_sequence, _ = self._load_queue_tail(recover=False)
        upper = min(tail_sequence, sequence + MAX_PENDING_BATCH)
        for candidate in range(sequence + 1, upper + 1):
            queue_ref = _read_json(self._queue_path(candidate))
            _validate_queue_ref(
                queue_ref,
                queue_sequence=candidate,
                previous_queue_entry_digest=previous_digest,
            )
            entry_digest = str(queue_ref["entry_digest"])
            if not _safe_regular_file_exists(self._ack_path(entry_digest)):
                state = self._read_attempt_state(entry_digest)
                if state is None or state["disposition"] != "manual-review":
                    break
            record = self._cursor_record_document(
                queue_sequence=candidate,
                previous_entry_digest=previous_digest,
                previous_cursor_digest=previous_cursor_digest,
                previous_acknowledged_count=acknowledged_count,
                previous_manual_review_count=manual_review_count,
            )
            _write_immutable_json(
                self._drain_cursor_record_path(candidate), record
            )
            self._validate_drain_cursor_record(
                record, expected_sequence=candidate
            )
            self._write_drain_cursor_pointer(record)
            sequence = candidate
            previous_digest = entry_digest
            previous_cursor_digest = str(record["cursor_digest"])
            acknowledged_count = int(record["acknowledged_count"])
            manual_review_count = int(record["manual_review_count"])

    def _assert_bounded_tail(self, entry: LifecycleSpoolEntry) -> None:
        if entry.sequence == 1:
            if entry.previous_entry_digest is not None:
                raise PolicyViolation("Lifecycle bounded tail first entry gecersiz")
            return
        assert entry.previous_entry_digest is not None
        previous = self._read_entry(entry.previous_entry_digest)
        if (
            previous is None
            or previous.client_id != entry.client_id
            or previous.session_id != entry.session_id
            or previous.sequence != entry.sequence - 1
        ):
            raise PolicyViolation("Lifecycle bounded session tail kopuk")

    def _delivery_document(
        self, entry: LifecycleSpoolEntry, *, queue_sequence: int
    ) -> dict[str, Any]:
        if queue_sequence < 1:
            raise ValidationFailed("Lifecycle delivery queue sequence pozitif olmali")
        body = {
            "schema": SPOOL_DELIVERY_REF_SCHEMA,
            "delivery_id": entry.delivery_id,
            "entry_digest": entry.entry_digest,
            "observation_digest": entry.observation_digest,
            "client_id": entry.client_id,
            "session_id": entry.session_id,
            "queue_sequence": queue_sequence,
            "grants_authority": False,
        }
        return body | {"ref_digest": digest(body)}

    def _queue_ref_document(
        self,
        entry: LifecycleSpoolEntry,
        *,
        queue_sequence: int,
        previous_queue_entry_digest: str | None,
    ) -> dict[str, Any]:
        if queue_sequence < 1 or (queue_sequence == 1) is not (
            previous_queue_entry_digest is None
        ):
            raise ValidationFailed("Lifecycle queue ref sequence/previous gecersiz")
        if previous_queue_entry_digest is not None:
            parse_digest(previous_queue_entry_digest)
        body = {
            "schema": SPOOL_QUEUE_REF_SCHEMA,
            "queue_sequence": queue_sequence,
            "entry_digest": entry.entry_digest,
            "previous_queue_entry_digest": previous_queue_entry_digest,
            "grants_authority": False,
        }
        return body | {"ref_digest": digest(body)}

    def _queue_state_document(
        self,
        entry: LifecycleSpoolEntry,
        *,
        queue_sequence: int,
        previous_queue_entry_digest: str | None,
        state: str,
    ) -> dict[str, Any]:
        if state not in {"pending", "committed"}:
            raise ValidationFailed("Lifecycle queue state gecersiz")
        body = {
            "schema": SPOOL_QUEUE_STATE_SCHEMA,
            "state": state,
            "tail_sequence": queue_sequence,
            "tail_entry_digest": entry.entry_digest,
            "previous_tail_sequence": queue_sequence - 1,
            "previous_tail_entry_digest": previous_queue_entry_digest,
            "pending_entry": entry.as_dict() if state == "pending" else None,
            "grants_authority": False,
        }
        return body | {"state_digest": digest(body)}

    def _checkpoint_document(
        self, entry: LifecycleSpoolEntry, *, state: str
    ) -> dict[str, Any]:
        if state not in {"pending", "committed"}:
            raise ValidationFailed("Lifecycle session checkpoint state gecersiz")
        body = {
            "schema": SPOOL_SESSION_CHECKPOINT_SCHEMA,
            "client_id": entry.client_id,
            "session_id": entry.session_id,
            "state": state,
            "sequence": entry.sequence,
            "entry_digest": entry.entry_digest,
            "previous_sequence": entry.sequence - 1,
            "previous_entry_digest": entry.previous_entry_digest,
            "delivery_id": entry.delivery_id,
            "pending_entry": entry.as_dict() if state == "pending" else None,
            "grants_authority": False,
        }
        return body | {"checkpoint_digest": digest(body)}

    def _ensure_write_directories(self) -> None:
        for path in (
            self.events_directory,
            self.acks_directory,
            self.attempts_directory,
            self.attempt_states_directory,
            self.deliveries_directory,
            self.sessions_directory,
            self.queue_directory,
            self.drain_cursors_directory,
        ):
            _ensure_safe_directory(path)

    def _entry_path(self, entry_digest: str) -> Path:
        return self.events_directory / f"{parse_digest(entry_digest)}.json"

    def _ack_path(self, entry_digest: str) -> Path:
        return self.acks_directory / f"{parse_digest(entry_digest)}.json"

    def _attempt_path(self, attempt_digest: str) -> Path:
        return self.attempts_directory / f"{parse_digest(attempt_digest)}.json"

    def _attempt_state_path(self, entry_digest: str) -> Path:
        return self.attempt_states_directory / f"{parse_digest(entry_digest)}.json"

    def _delivery_path(self, delivery_id: str) -> Path:
        return self.deliveries_directory / f"{parse_digest(delivery_id)}.json"

    def _queue_path(self, queue_sequence: int) -> Path:
        if queue_sequence < 1:
            raise ValidationFailed("Lifecycle queue sequence pozitif olmali")
        return self.queue_directory / f"{queue_sequence:020d}.json"

    def _drain_cursor_record_path(self, queue_sequence: int) -> Path:
        if queue_sequence < 1:
            raise ValidationFailed("Lifecycle drain cursor sequence pozitif olmali")
        return self.drain_cursors_directory / f"{queue_sequence:020d}.json"

    def _session_path(self, client_id: str, session_id: str) -> Path:
        session_key = digest({"client_id": client_id, "session_id": session_id})
        return self.sessions_directory / f"{parse_digest(session_key)}.json"


def replay_pending(
    spool: ClientLifecycleSpool,
    *,
    deliver: Callable[[LifecycleSpoolEntry], CanonicalLifecycleReceipt],
    limit: int = 80,
    attempted_at: dt.datetime | None = None,
) -> tuple[LifecycleReplayResult, ...]:
    """Replay one explicit bounded batch through an idempotent canonical callback.

    ``deliver`` must return a receipt whose exact canonical event was persisted
    and then looked up idempotently.  This function does not schedule itself,
    open a database, retry, or infer authorization.
    """

    at = attempted_at or _utc_now()
    _timestamp(at, label="attempted_at")
    results: list[LifecycleReplayResult] = []
    for entry in spool.pending(limit=limit):
        if entry.previous_entry_digest is not None and not _safe_regular_file_exists(
            spool._ack_path(entry.previous_entry_digest)
        ):
            predecessor_state = spool._read_attempt_state(
                entry.previous_entry_digest
            )
            if (
                predecessor_state is not None
                and predecessor_state["disposition"] == "manual-review"
            ):
                attempt = spool.record_predecessor_manual_review(
                    entry.entry_digest,
                    attempted_at=at,
                )
                results.append(
                    LifecycleReplayResult(
                        entry.entry_digest,
                        "recovery-required",
                        None,
                        attempt["attempt_digest"],
                    )
                )
                continue
        try:
            receipt = deliver(entry)
            receipt.assert_binding(entry)
        except (ZekamError, OSError) as exc:
            category = exc.code if isinstance(exc, ZekamError) else "io-error"
            outcome = (
                "rejected"
                if isinstance(exc, (PolicyViolation, ValidationFailed))
                else "failed"
            )
            attempt = spool.record_attempt(
                entry.entry_digest,
                outcome=outcome,
                evidence_digest=digest(
                    {
                        "schema": "zekam-client-lifecycle-replay-failure/v1",
                        "entry_digest": entry.entry_digest,
                        "failure_category": category,
                    }
                ),
                attempted_at=at,
            )
            results.append(
                LifecycleReplayResult(
                    entry.entry_digest,
                    (
                        "recovery-required"
                        if attempt["disposition"] == "manual-review"
                        else outcome
                    ),
                    None,
                    attempt["attempt_digest"],
                )
            )
            continue
        except Exception:
            # Unexpected implementation failures remain visible without
            # persisting exception text, repr, paths, payloads or secrets.
            attempt = spool.record_attempt(
                entry.entry_digest,
                outcome="failed",
                evidence_digest=digest(
                    {
                        "schema": "zekam-client-lifecycle-replay-failure/v1",
                        "entry_digest": entry.entry_digest,
                        "failure_category": "unexpected-exception",
                    }
                ),
                attempted_at=at,
            )
            results.append(
                LifecycleReplayResult(
                    entry.entry_digest,
                    (
                        "recovery-required"
                        if attempt["disposition"] == "manual-review"
                        else "failed"
                    ),
                    None,
                    attempt["attempt_digest"],
                )
            )
            continue

        attempt = spool.record_attempt(
            entry.entry_digest,
            outcome="completed",
            evidence_digest=receipt.canonical_lookup_digest,
            attempted_at=at,
        )
        spool._acknowledge_verified_receipt(
            entry,
            receipt=receipt,
            acknowledged_at=at,
        )
        results.append(
            LifecycleReplayResult(
                entry.entry_digest,
                "completed",
                receipt.canonical_ack_digest,
                attempt["attempt_digest"],
            )
        )
    return tuple(results)


def canonical_lifecycle_event(
    entry: LifecycleSpoolEntry,
    *,
    client_instance_id: str,
    previous_canonical_event_digest: str | None,
) -> dict[str, Any]:
    """Project one content-free spool entry to the governed PostgreSQL schema."""

    entry.assert_integrity()
    if not _CLIENT_INSTANCE.fullmatch(client_instance_id):
        raise ValidationFailed("Lifecycle client_instance_id canonical degil")
    if entry.sequence == 1:
        if previous_canonical_event_digest is not None:
            raise PolicyViolation("Lifecycle first canonical event previous tasiyamaz")
    else:
        if previous_canonical_event_digest is None:
            raise PolicyViolation("Lifecycle canonical predecessor receipt bekleniyor")
        parse_digest(previous_canonical_event_digest)
    try:
        ledger_event_type = _LEDGER_EVENT_TYPES[entry.internal_event_type]
    except KeyError as exc:
        raise PolicyViolation("Lifecycle canonical ledger event mapping eksik") from exc
    body = {
        "schema": CANONICAL_LIFECYCLE_EVENT_SCHEMA,
        "client_id": client_instance_id,
        "client_kind": entry.client_kind,
        "session_id": entry.session_id,
        "sequence": entry.sequence,
        "previous_digest": previous_canonical_event_digest,
        "event_type": ledger_event_type,
        "payload_digest": entry.observation_digest,
        "occurred_at": _timestamp(entry.occurred_at, label="occurred_at"),
        "transcript_included": False,
        "grants_authority": False,
    }
    return body | {"event_digest": digest(body)}


def drain_to_postgres(
    spool: ClientLifecycleSpool,
    *,
    client_instance_id: str,
    continuity_admission: LifecycleContinuityAdmission | None = None,
    limit: int = 80,
    attempted_at: dt.datetime | None = None,
) -> tuple[LifecycleReplayResult, ...]:
    """Explicit bounded production drain with exact DB receipt re-read.

    The adapter must apply canonical ingest plus continuity admission in one
    transaction, then expose a separate read-only lookup. This function never
    calls the generic repository directly, so generic commit cannot precede
    governed admission.
    """

    at = attempted_at or _utc_now()
    _timestamp(at, label="attempted_at")
    if continuity_admission is None:
        raise PolicyViolation(
            "Lifecycle drain governed continuity admission adapter ister"
        )
    if spool.client_instance_id() != client_instance_id:
        raise PolicyViolation("Lifecycle drain client instance binding mismatch")

    def deliver(entry: LifecycleSpoolEntry) -> CanonicalLifecycleReceipt:
        canonical_event = canonical_lifecycle_event(
            entry,
            client_instance_id=client_instance_id,
            previous_canonical_event_digest=spool.previous_canonical_event_digest(entry),
        )
        preflight = continuity_admission.preflight(
            entry,
            canonical_event,
            client_instance_id=client_instance_id,
        )
        checked_preflight = _validate_continuity_preflight(
            preflight,
            entry=entry,
            canonical_event_digest=str(canonical_event["event_digest"]),
            client_instance_id=client_instance_id,
        )
        applied = continuity_admission.apply(
            entry,
            canonical_event,
            preflight=checked_preflight,
            client_instance_id=client_instance_id,
            now=at,
        )
        applied.assert_binding(entry)
        _assert_preflight_receipt_binding(checked_preflight, applied)
        lookup = continuity_admission.lookup(
            entry,
            canonical_event,
            preflight=checked_preflight,
            client_instance_id=client_instance_id,
        )
        lookup.assert_binding(entry)
        _assert_preflight_receipt_binding(checked_preflight, lookup)
        if applied != lookup:
            raise PolicyViolation("Lifecycle continuity terminal lookup drift")
        return lookup

    return replay_pending(
        spool,
        deliver=deliver,
        limit=limit,
        attempted_at=at,
    )


def _validate_ack(document: Any, *, entry_digest: str) -> None:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _ACK_KEYS
        or document.get("schema") != SPOOL_ACK_SCHEMA
    ):
        raise ValidationFailed("Lifecycle ACK schema gecersiz")
    if (
        document.get("entry_digest") != entry_digest
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle ACK binding/authority gecersiz")
    canonical_ack_digest = document.get("canonical_ack_digest")
    canonical_event_digest = document.get("canonical_event_digest")
    canonical_event_id = document.get("canonical_event_id")
    canonical_lookup_digest = document.get("canonical_lookup_digest")
    runtime_binding_id = document.get("runtime_binding_id")
    runtime_binding_digest = document.get("runtime_binding_digest")
    continuity_binding = document.get("continuity_binding")
    ack_digest = document.get("ack_digest")
    if not all(
        isinstance(value, str)
        for value in (
            canonical_ack_digest,
            canonical_event_digest,
            canonical_event_id,
            canonical_lookup_digest,
            ack_digest,
        )
    ):
        raise ValidationFailed("Lifecycle ACK digest alanlari eksik")
    parse_digest(canonical_ack_digest)
    parse_digest(canonical_event_digest)
    parse_digest(canonical_lookup_digest)
    parse_digest(ack_digest)
    try:
        if str(UUID(canonical_event_id)) != canonical_event_id:
            raise ValueError
    except ValueError as exc:
        raise ValidationFailed("Lifecycle ACK canonical event_id UUID olmali") from exc
    if (runtime_binding_id is None) is not (runtime_binding_digest is None):
        raise PolicyViolation("Lifecycle ACK runtime binding alanlari birlikte olmali")
    if runtime_binding_id is not None:
        if not isinstance(runtime_binding_id, str) or not isinstance(
            runtime_binding_digest, str
        ):
            raise ValidationFailed("Lifecycle ACK runtime binding alanlari gecersiz")
        try:
            if str(UUID(runtime_binding_id)) != runtime_binding_id:
                raise ValueError
        except ValueError as exc:
            raise ValidationFailed("Lifecycle ACK runtime binding UUID olmali") from exc
        parse_digest(runtime_binding_digest)
    if not isinstance(continuity_binding, dict):
        raise ValidationFailed("Lifecycle ACK continuity binding eksik")
    if (
        continuity_binding.get("entry_digest") != entry_digest
        or continuity_binding.get("canonical_event_digest") != canonical_event_digest
    ):
        raise PolicyViolation("Lifecycle ACK continuity binding mismatch")
    binding_digest = continuity_binding.get("binding_digest")
    if not isinstance(binding_digest, str):
        raise ValidationFailed("Lifecycle ACK continuity binding digest eksik")
    parse_digest(binding_digest)
    binding_body = {
        key: value
        for key, value in continuity_binding.items()
        if key != "binding_digest"
    }
    if (
        frozenset(continuity_binding) != _CONTINUITY_BINDING_KEYS
        or continuity_binding.get("schema") != CONTINUITY_BINDING_SCHEMA
        or continuity_binding.get("status") != "completed"
        or continuity_binding.get("grants_authority") is not False
        or digest(binding_body) != binding_digest
    ):
        raise PolicyViolation("Lifecycle ACK continuity binding schema/digest gecersiz")
    for key in (
        "continuity_event_digest",
        "effect_receipt_digest",
        "terminal_receipt_digest",
    ):
        _digest_text(
            continuity_binding.get(key), label=f"ACK continuity binding {key}"
        )
    for key in (
        "realm_id",
        "project_id",
        "work_item_id",
        "run_id",
        "authorization_id",
        "job_id",
        "claim_id",
        "effect_receipt_id",
        "continuity_event_id",
        "delivery_outbox_id",
    ):
        raw = continuity_binding.get(key)
        if not isinstance(raw, str):
            raise ValidationFailed(f"ACK continuity binding {key} UUID olmali")
        try:
            if str(UUID(raw)) != raw:
                raise ValueError
        except ValueError as exc:
            raise ValidationFailed(
                f"ACK continuity binding {key} UUID olmali"
            ) from exc
    compiler_enqueue = continuity_binding.get("compiler_enqueue")
    if not isinstance(compiler_enqueue, bool):
        raise ValidationFailed("ACK continuity compiler_enqueue boolean olmali")
    if (
        continuity_binding.get("event_type") == "pre_compaction"
        and not compiler_enqueue
    ):
        raise PolicyViolation("ACK pre-compaction compiler enqueue ister")
    _parse_timestamp(document.get("acknowledged_at"), label="acknowledged_at")
    body = {key: value for key, value in document.items() if key != "ack_digest"}
    if digest(body) != ack_digest:
        raise PolicyViolation("Lifecycle ACK digest mismatch")
    expected_lookup_digest = digest(
        {
            "schema": "zekam-client-lifecycle-canonical-lookup/v1",
            "entry_digest": entry_digest,
            "event_digest": canonical_event_digest,
            "event_id": canonical_event_id,
            "canonical_ack_digest": canonical_ack_digest,
            "runtime_binding_id": runtime_binding_id,
            "runtime_binding_digest": runtime_binding_digest,
            "lookup_verified": True,
            "grants_authority": False,
        }
    )
    if canonical_lookup_digest != expected_lookup_digest:
        raise PolicyViolation("Lifecycle ACK canonical lookup digest mismatch")


def _validate_delivery_ref(document: Any, *, delivery_id: str) -> None:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _DELIVERY_REF_KEYS
        or document.get("schema") != SPOOL_DELIVERY_REF_SCHEMA
        or document.get("delivery_id") != delivery_id
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle delivery ref schema/binding gecersiz")
    for key in ("delivery_id", "entry_digest", "observation_digest", "ref_digest"):
        _digest_text(document.get(key), label=f"delivery ref {key}")
    _safe_text(document.get("client_id"), label="delivery ref client_id")
    _safe_text(document.get("session_id"), label="delivery ref session_id")
    queue_sequence = document.get("queue_sequence")
    if (
        not isinstance(queue_sequence, int)
        or isinstance(queue_sequence, bool)
        or queue_sequence < 1
    ):
        raise ValidationFailed("Lifecycle delivery ref queue sequence gecersiz")
    body = {key: value for key, value in document.items() if key != "ref_digest"}
    if digest(body) != document["ref_digest"]:
        raise PolicyViolation("Lifecycle delivery ref digest mismatch")


def _validate_queue_ref(
    document: Any,
    *,
    queue_sequence: int,
    previous_queue_entry_digest: str | None,
) -> None:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _QUEUE_REF_KEYS
        or document.get("schema") != SPOOL_QUEUE_REF_SCHEMA
        or document.get("queue_sequence") != queue_sequence
        or document.get("previous_queue_entry_digest")
        != previous_queue_entry_digest
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle queue ref schema/chain binding gecersiz")
    if queue_sequence < 1 or (queue_sequence == 1) is not (
        previous_queue_entry_digest is None
    ):
        raise ValidationFailed("Lifecycle queue ref sequence/previous gecersiz")
    if previous_queue_entry_digest is not None:
        parse_digest(previous_queue_entry_digest)
    _digest_text(document.get("entry_digest"), label="queue ref entry_digest")
    ref_digest = _digest_text(document.get("ref_digest"), label="queue ref_digest")
    body = {key: value for key, value in document.items() if key != "ref_digest"}
    if digest(body) != ref_digest:
        raise PolicyViolation("Lifecycle queue ref digest mismatch")


def _validate_queue_state(document: Any) -> None:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _QUEUE_STATE_KEYS
        or document.get("schema") != SPOOL_QUEUE_STATE_SCHEMA
        or document.get("state") not in {"pending", "committed"}
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle queue state schema/authority gecersiz")
    sequence = document.get("tail_sequence")
    previous_sequence = document.get("previous_tail_sequence")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or previous_sequence != sequence - 1
    ):
        raise ValidationFailed("Lifecycle queue state sequence gecersiz")
    tail_digest = _digest_text(
        document.get("tail_entry_digest"), label="queue tail_entry_digest"
    )
    previous = document.get("previous_tail_entry_digest")
    if (sequence == 1) is not (previous is None):
        raise PolicyViolation("Lifecycle queue state previous binding gecersiz")
    if previous is not None:
        _digest_text(previous, label="queue previous_tail_entry_digest")
    pending = document.get("pending_entry")
    if document["state"] == "pending":
        entry = _entry_from_document(pending)
        if entry.entry_digest != tail_digest:
            raise PolicyViolation("Lifecycle queue pending entry binding mismatch")
    elif pending is not None:
        raise PolicyViolation("Lifecycle committed queue pending entry tasiyamaz")
    state_digest = _digest_text(document.get("state_digest"), label="queue state_digest")
    body = {key: value for key, value in document.items() if key != "state_digest"}
    if digest(body) != state_digest:
        raise PolicyViolation("Lifecycle queue state digest mismatch")


def _validate_drain_cursor_record(
    document: Any,
    *,
    expected_sequence: int,
) -> None:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _DRAIN_CURSOR_KEYS
        or document.get("schema") != SPOOL_DRAIN_CURSOR_SCHEMA
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle drain cursor schema/authority gecersiz")
    sequence = document.get("queue_sequence")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence != expected_sequence
        or sequence < 1
    ):
        raise ValidationFailed("Lifecycle drain cursor sequence gecersiz")
    for key in (
        "entry_digest",
        "queue_ref_digest",
        "delivery_ref_digest",
        "attempt_state_digest",
        "attempt_ref",
        "attempt_digest",
        "cursor_digest",
    ):
        _digest_text(document.get(key), label=f"drain cursor {key}")
    disposition = document.get("terminal_disposition")
    if not isinstance(disposition, str) or disposition not in {
        "acknowledged",
        "manual-review",
    }:
        raise ValidationFailed("Lifecycle drain cursor terminal disposition gecersiz")
    ack_values = tuple(
        document.get(key)
        for key in (
            "ack_digest",
            "canonical_event_digest",
            "continuity_binding_digest",
        )
    )
    if disposition == "acknowledged":
        for key, value in zip(
            ("ack_digest", "canonical_event_digest", "continuity_binding_digest"),
            ack_values,
            strict=True,
        ):
            _digest_text(value, label=f"drain cursor {key}")
    elif ack_values != (None, None, None):
        raise PolicyViolation("Lifecycle manual-review cursor ACK alani tasiyamaz")
    acknowledged_count = document.get("acknowledged_count")
    manual_review_count = document.get("manual_review_count")
    if (
        not isinstance(acknowledged_count, int)
        or isinstance(acknowledged_count, bool)
        or acknowledged_count < 0
        or not isinstance(manual_review_count, int)
        or isinstance(manual_review_count, bool)
        or manual_review_count < 0
        or acknowledged_count + manual_review_count != sequence
    ):
        raise PolicyViolation("Lifecycle drain cursor terminal sayaclari gecersiz")
    for key in ("previous_entry_digest", "previous_cursor_digest"):
        value = document.get(key)
        if value is not None:
            _digest_text(value, label=f"drain cursor {key}")
    previous_pair = (
        document.get("previous_entry_digest"),
        document.get("previous_cursor_digest"),
    )
    if (sequence == 1 and previous_pair != (None, None)) or (
        sequence > 1 and any(value is None for value in previous_pair)
    ):
        raise PolicyViolation("Lifecycle drain cursor previous binding gecersiz")
    cursor_digest = _digest_text(
        document.get("cursor_digest"), label="drain cursor_digest"
    )
    body = {key: value for key, value in document.items() if key != "cursor_digest"}
    if digest(body) != cursor_digest:
        raise PolicyViolation("Lifecycle drain cursor digest mismatch")


def _validate_drain_cursor_pointer(document: Any) -> None:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _DRAIN_CURSOR_POINTER_KEYS
        or document.get("schema") != SPOOL_DRAIN_CURSOR_POINTER_SCHEMA
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle drain cursor pointer schema gecersiz")
    sequence = document.get("queue_sequence")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
    ):
        raise ValidationFailed("Lifecycle drain cursor pointer sequence gecersiz")
    for key in ("entry_digest", "cursor_digest", "pointer_digest"):
        _digest_text(document.get(key), label=f"drain cursor pointer {key}")
    acknowledged_count = document.get("acknowledged_count")
    manual_review_count = document.get("manual_review_count")
    if (
        not isinstance(acknowledged_count, int)
        or isinstance(acknowledged_count, bool)
        or acknowledged_count < 0
        or not isinstance(manual_review_count, int)
        or isinstance(manual_review_count, bool)
        or manual_review_count < 0
        or acknowledged_count + manual_review_count != sequence
    ):
        raise PolicyViolation("Lifecycle drain cursor pointer sayaclari gecersiz")
    body = {key: value for key, value in document.items() if key != "pointer_digest"}
    if digest(body) != document["pointer_digest"]:
        raise PolicyViolation("Lifecycle drain cursor pointer digest mismatch")


def _validate_checkpoint(
    document: Any, *, client_id: str, session_id: str
) -> None:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _CHECKPOINT_KEYS
        or document.get("schema") != SPOOL_SESSION_CHECKPOINT_SCHEMA
        or document.get("client_id") != client_id
        or document.get("session_id") != session_id
        or document.get("state") not in {"pending", "committed"}
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle session checkpoint schema/binding gecersiz")
    sequence = document.get("sequence")
    previous_sequence = document.get("previous_sequence")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or previous_sequence != sequence - 1
    ):
        raise ValidationFailed("Lifecycle session checkpoint sequence gecersiz")
    for key in ("entry_digest", "delivery_id", "checkpoint_digest"):
        _digest_text(document.get(key), label=f"checkpoint {key}")
    previous = document.get("previous_entry_digest")
    if (sequence == 1) is not (previous is None):
        raise PolicyViolation("Lifecycle session checkpoint previous binding gecersiz")
    if previous is not None:
        _digest_text(previous, label="checkpoint previous_entry_digest")
    pending = document.get("pending_entry")
    if document["state"] == "pending":
        entry = _entry_from_document(pending)
        if (
            entry.entry_digest != document["entry_digest"]
            or entry.delivery_id != document["delivery_id"]
            or entry.sequence != sequence
            or entry.previous_entry_digest != previous
            or entry.client_id != client_id
            or entry.session_id != session_id
        ):
            raise PolicyViolation("Lifecycle pending checkpoint entry binding mismatch")
    elif pending is not None:
        raise PolicyViolation("Lifecycle committed checkpoint pending entry tasiyamaz")
    body = {key: value for key, value in document.items() if key != "checkpoint_digest"}
    if digest(body) != document["checkpoint_digest"]:
        raise PolicyViolation("Lifecycle session checkpoint digest mismatch")


def _validate_attempt(
    document: Any,
    *,
    entry_digest: str,
    expected_retry_key: str | None = None,
) -> None:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _ATTEMPT_KEYS
        or document.get("schema") != SPOOL_ATTEMPT_SCHEMA
        or document.get("entry_digest") != entry_digest
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle attempt schema/binding gecersiz")
    for key in (
        "entry_digest",
        "evidence_digest",
        "retry_key",
        "attempt_digest",
    ):
        _digest_text(document.get(key), label=f"attempt {key}")
    if (
        expected_retry_key is not None
        and document["retry_key"] != expected_retry_key
    ):
        raise PolicyViolation("Lifecycle attempt retry ref mismatch")
    _parse_timestamp(document.get("attempted_at"), label="attempted_at")
    outcome = document.get("outcome")
    if not isinstance(outcome, str) or outcome not in {
        "completed",
        "deferred",
        "failed",
        "rejected",
    }:
        raise ValidationFailed("Lifecycle attempt outcome canonical degil")
    attempt_number = document.get("attempt_number")
    failure_count = document.get("failure_count")
    disposition = document.get("disposition")
    if (
        not isinstance(attempt_number, int)
        or isinstance(attempt_number, bool)
        or attempt_number < 1
        or not isinstance(failure_count, int)
        or isinstance(failure_count, bool)
        or failure_count < 0
        or failure_count > attempt_number
        or not isinstance(disposition, str)
        or disposition not in {"completed", "retryable", "manual-review"}
    ):
        raise ValidationFailed("Lifecycle attempt bounded state gecersiz")
    if (outcome == "completed") is not (disposition == "completed"):
        raise PolicyViolation("Lifecycle attempt outcome/disposition mismatch")
    terminal_reason = document.get("terminal_reason")
    predecessor_entry_digest = document.get("predecessor_entry_digest")
    predecessor_attempt_state_digest = document.get(
        "predecessor_attempt_state_digest"
    )
    if disposition == "manual-review":
        if terminal_reason == "retry-budget-exhausted":
            if failure_count < MAX_REPLAY_FAILURES:
                raise PolicyViolation("Lifecycle attempt manual-review siniri gecersiz")
        elif terminal_reason == "predecessor-manual-review":
            if outcome != "rejected" or failure_count < 1:
                raise PolicyViolation(
                    "Lifecycle predecessor manual-review sonucu gecersiz"
                )
            _digest_text(
                predecessor_entry_digest,
                label="attempt predecessor_entry_digest",
            )
            _digest_text(
                predecessor_attempt_state_digest,
                label="attempt predecessor_attempt_state_digest",
            )
            expected_evidence_digest = digest(
                {
                    "schema": "zekam-client-lifecycle-predecessor-block/v1",
                    "entry_digest": entry_digest,
                    "predecessor_entry_digest": predecessor_entry_digest,
                    "predecessor_attempt_state_digest": (
                        predecessor_attempt_state_digest
                    ),
                    "grants_authority": False,
                }
            )
            if document["evidence_digest"] != expected_evidence_digest:
                raise PolicyViolation(
                    "Lifecycle predecessor manual-review evidence mismatch"
                )
        else:
            raise PolicyViolation("Lifecycle attempt terminal reason gecersiz")
    elif terminal_reason is not None:
        raise PolicyViolation("Lifecycle non-terminal attempt reason tasiyamaz")
    if terminal_reason != "predecessor-manual-review" and (
        predecessor_entry_digest is not None
        or predecessor_attempt_state_digest is not None
    ):
        raise PolicyViolation("Lifecycle attempt beklenmeyen predecessor tasiyor")
    retry_body = {
        "entry_digest": entry_digest,
        "outcome": outcome,
        "evidence_digest": document["evidence_digest"],
    }
    if terminal_reason == "predecessor-manual-review":
        retry_body["terminal_reason"] = terminal_reason
    if document["retry_key"] != digest(retry_body):
        raise PolicyViolation("Lifecycle attempt deterministic retry key mismatch")
    if disposition == "retryable" and failure_count >= MAX_REPLAY_FAILURES:
        raise PolicyViolation("Lifecycle attempt retry siniri asildi")
    body = {key: value for key, value in document.items() if key != "attempt_digest"}
    if digest(body) != document["attempt_digest"]:
        raise PolicyViolation("Lifecycle attempt digest mismatch")


def _validate_attempt_state(document: Any, *, entry_digest: str) -> None:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _ATTEMPT_STATE_KEYS
        or document.get("schema") != SPOOL_ATTEMPT_STATE_SCHEMA
        or document.get("entry_digest") != entry_digest
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle attempt state schema/binding gecersiz")
    attempt_count = document.get("attempt_count")
    failure_count = document.get("failure_count")
    disposition = document.get("disposition")
    if (
        not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count < 1
        or not isinstance(failure_count, int)
        or isinstance(failure_count, bool)
        or failure_count < 0
        or failure_count > attempt_count
        or not isinstance(disposition, str)
        or disposition not in {"completed", "retryable", "manual-review"}
    ):
        raise ValidationFailed("Lifecycle attempt state sayac/disposition gecersiz")
    for key in (
        "entry_digest",
        "latest_attempt_digest",
        "latest_attempt_ref",
        "latest_retry_key",
        "state_digest",
    ):
        _digest_text(document.get(key), label=f"attempt state {key}")
    terminal_reason = document.get("terminal_reason")
    predecessor_entry_digest = document.get("predecessor_entry_digest")
    predecessor_attempt_state_digest = document.get(
        "predecessor_attempt_state_digest"
    )
    if disposition == "manual-review":
        if terminal_reason == "retry-budget-exhausted":
            if failure_count < MAX_REPLAY_FAILURES:
                raise PolicyViolation(
                    "Lifecycle attempt state manual-review siniri gecersiz"
                )
        elif terminal_reason == "predecessor-manual-review":
            if failure_count < 1:
                raise PolicyViolation(
                    "Lifecycle predecessor attempt state gecersiz"
                )
            _digest_text(
                predecessor_entry_digest,
                label="attempt state predecessor_entry_digest",
            )
            _digest_text(
                predecessor_attempt_state_digest,
                label="attempt state predecessor_attempt_state_digest",
            )
        else:
            raise PolicyViolation("Lifecycle attempt state terminal reason gecersiz")
    elif terminal_reason is not None:
        raise PolicyViolation("Lifecycle non-terminal attempt state reason tasiyamaz")
    if terminal_reason != "predecessor-manual-review" and (
        predecessor_entry_digest is not None
        or predecessor_attempt_state_digest is not None
    ):
        raise PolicyViolation("Lifecycle attempt state beklenmeyen predecessor tasiyor")
    if disposition == "retryable" and failure_count >= MAX_REPLAY_FAILURES:
        raise PolicyViolation("Lifecycle attempt state retry siniri asildi")
    if document["latest_attempt_ref"] != document["latest_retry_key"]:
        raise PolicyViolation("Lifecycle attempt state retry ref mismatch")
    body = {key: value for key, value in document.items() if key != "state_digest"}
    if digest(body) != document["state_digest"]:
        raise PolicyViolation("Lifecycle attempt state digest mismatch")


def _validate_instance(document: Any, client_id: str) -> str:
    if (
        not isinstance(document, dict)
        or frozenset(document) != _INSTANCE_KEYS
        or document.get("schema") != SPOOL_INSTANCE_SCHEMA
        or document.get("client_id") != client_id
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Lifecycle client instance schema/binding gecersiz")
    value = document.get("client_instance_id")
    if not isinstance(value, str) or _CLIENT_INSTANCE.fullmatch(value) is None:
        raise ValidationFailed("Lifecycle client instance kimligi gecersiz")
    instance_digest = _digest_text(document.get("instance_digest"), label="instance_digest")
    body = {key: item for key, item in document.items() if key != "instance_digest"}
    if digest(body) != instance_digest:
        raise PolicyViolation("Lifecycle client instance digest mismatch")
    return value


def _read_json(path: Path) -> Any:
    try:
        return json.loads(_read_bounded_bytes(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Lifecycle spool document okunamadi") from exc


def _assert_json_directory(path: Path, *, label: str) -> None:
    if not _safe_directory_exists(path):
        raise PolicyViolation(f"Lifecycle {label} spool dizini gecersiz")
    _safe_json_files(path, label=label)


def _is_reparse_or_symlink(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _absolute_no_resolve(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_safe_parent_chain(path: Path) -> None:
    absolute = _absolute_no_resolve(path)
    current = Path(absolute.anchor)
    try:
        anchor_info = os.lstat(current)
    except OSError as exc:
        raise ValidationFailed("Lifecycle spool anchor okunamadi") from exc
    if _is_reparse_or_symlink(anchor_info) or not stat.S_ISDIR(anchor_info.st_mode):
        raise PolicyViolation("Lifecycle spool anchor reparse/symlink olamaz")
    missing = False
    for part in absolute.parts[1:-1]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            missing = True
            continue
        except OSError as exc:
            raise ValidationFailed("Lifecycle spool parent okunamadi") from exc
        if missing:
            raise PolicyViolation("Lifecycle spool parent chain race algilandi")
        if _is_reparse_or_symlink(info) or not stat.S_ISDIR(info.st_mode):
            raise PolicyViolation("Lifecycle spool parent reparse/symlink olamaz")


def _safe_directory_exists(path: Path) -> bool:
    _assert_safe_parent_chain(path)
    try:
        info = os.lstat(_absolute_no_resolve(path))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValidationFailed("Lifecycle spool dizini okunamadi") from exc
    if _is_reparse_or_symlink(info) or not stat.S_ISDIR(info.st_mode):
        raise PolicyViolation("Lifecycle spool dizini reparse/symlink olamaz")
    return True


def _ensure_safe_directory(path: Path) -> None:
    _assert_safe_parent_chain(path)
    try:
        _absolute_no_resolve(path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValidationFailed("Lifecycle spool dizini olusturulamadi") from exc
    if not _safe_directory_exists(path):
        raise PolicyViolation("Lifecycle spool dizini olusturulamadi")


def _safe_regular_file_exists(path: Path, *, max_bytes: int = MAX_SPOOL_DOCUMENT_BYTES) -> bool:
    _assert_safe_parent_chain(path)
    try:
        info = os.lstat(_absolute_no_resolve(path))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValidationFailed("Lifecycle spool dosyasi okunamadi") from exc
    if _is_reparse_or_symlink(info) or not stat.S_ISREG(info.st_mode):
        raise PolicyViolation("Lifecycle spool target regular file olmali")
    if info.st_size < 0 or info.st_size > max_bytes:
        raise PolicyViolation("Lifecycle spool target boyut sinirini asti")
    return True


def _assert_fd_matches_path(fd: int, path: Path, *, max_bytes: int) -> os.stat_result:
    opened = os.fstat(fd)
    if (
        _is_reparse_or_symlink(opened)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_size < 0
        or opened.st_size > max_bytes
    ):
        raise PolicyViolation("Lifecycle spool opened target gecersiz")
    current = os.lstat(_absolute_no_resolve(path))
    if _is_reparse_or_symlink(current) or not stat.S_ISREG(current.st_mode):
        raise PolicyViolation("Lifecycle spool target open sonrasi drift")
    if opened.st_ino and current.st_ino and (
        opened.st_dev != current.st_dev or opened.st_ino != current.st_ino
    ):
        raise PolicyViolation("Lifecycle spool target identity drift")
    return opened


def _read_bounded_bytes(path: Path) -> bytes:
    if not _safe_regular_file_exists(path):
        raise ValidationFailed("Lifecycle spool document bulunamadi")
    flags = os.O_RDONLY | _OPEN_BINARY | _OPEN_NOFOLLOW
    try:
        fd = os.open(_absolute_no_resolve(path), flags)
    except OSError as exc:
        raise ValidationFailed("Lifecycle spool document acilamadi") from exc
    try:
        _assert_fd_matches_path(fd, path, max_bytes=MAX_SPOOL_DOCUMENT_BYTES)
        chunks: list[bytes] = []
        remaining = MAX_SPOOL_DOCUMENT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_SPOOL_DOCUMENT_BYTES:
            raise PolicyViolation("Lifecycle spool document boyut sinirini asti")
        return payload
    finally:
        os.close(fd)


def _safe_json_files(path: Path, *, label: str = "document") -> tuple[Path, ...]:
    if not _safe_directory_exists(path):
        return ()
    try:
        with os.scandir(_absolute_no_resolve(path)) as entries:
            found: list[Path] = []
            for item in entries:
                info = item.stat(follow_symlinks=False)
                if (
                    _is_reparse_or_symlink(info)
                    or not stat.S_ISREG(info.st_mode)
                    or Path(item.name).suffix != ".json"
                    or info.st_size > MAX_SPOOL_DOCUMENT_BYTES
                ):
                    raise PolicyViolation(
                        f"Lifecycle {label} spool beklenmeyen artifact iceriyor"
                    )
                found.append(path / item.name)
    except OSError as exc:
        raise ValidationFailed(f"Lifecycle {label} spool dizini okunamadi") from exc
    return tuple(sorted(found))


def _write_new_file(path: Path, payload: bytes) -> None:
    _assert_safe_parent_chain(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_BINARY | _OPEN_NOFOLLOW
    try:
        fd = os.open(_absolute_no_resolve(path), flags, 0o600)
    except OSError as exc:
        raise ValidationFailed("Lifecycle spool temporary yazimi acilamadi") from exc
    try:
        _assert_fd_matches_path(fd, path, max_bytes=MAX_SPOOL_DOCUMENT_BYTES)
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_parent_directory(path: Path) -> bool:
    """Durably flush the parent where the platform exposes directory fsync.

    CPython on Windows does not expose a no-follow directory handle suitable
    for ``os.fsync``. Windows therefore relies on flushed file handles plus
    atomic link/replace; the missing directory-flush guarantee is explicit.
    """

    parent = _absolute_no_resolve(path).parent
    _safe_directory_exists(parent)
    if os.name == "nt":
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _OPEN_NOFOLLOW
    try:
        fd = os.open(parent, flags)
    except OSError as exc:
        raise ValidationFailed("Lifecycle spool parent fsync acilamadi") from exc
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def _safe_remove_temporary(path: Path) -> None:
    try:
        exists = _safe_regular_file_exists(path)
    except FileNotFoundError:
        return
    if not exists:
        return
    try:
        os.unlink(_absolute_no_resolve(path))
        _fsync_parent_directory(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValidationFailed("Lifecycle spool temporary silinemedi") from exc


def _link_immutable_no_follow(source: Path, target: Path) -> None:
    try:
        os.link(
            _absolute_no_resolve(source),
            _absolute_no_resolve(target),
            follow_symlinks=False,
        )
    except (NotImplementedError, TypeError):
        if os.name != "nt":
            raise
        # Windows builds that do not expose follow_symlinks=False still get
        # lstat/reparse and opened-file identity checks on the source plus a
        # safe parent chain and missing target check. This is best-effort, not
        # a kernel no-follow guarantee.
        _safe_regular_file_exists(source)
        if os.path.lexists(target):
            _safe_regular_file_exists(target)
        _assert_safe_parent_chain(target)
        os.link(_absolute_no_resolve(source), _absolute_no_resolve(target))


def _write_immutable_json(path: Path, document: Mapping[str, Any]) -> None:
    payload = canonical_bytes(dict(document)) + b"\n"
    if len(payload) > MAX_SPOOL_DOCUMENT_BYTES:
        raise PolicyViolation("Lifecycle spool document boyut sinirini asti")
    if _safe_regular_file_exists(path):
        existing = _read_bounded_bytes(path)
        if existing != payload:
            raise PolicyViolation("Lifecycle immutable spool collision")
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _write_new_file(temporary, payload)
        try:
            _link_immutable_no_follow(temporary, path)
        except FileExistsError:
            if not _safe_regular_file_exists(path) or _read_bounded_bytes(path) != payload:
                raise ConcurrencyConflict("Lifecycle spool concurrent immutable write")
        _safe_regular_file_exists(path)
        _fsync_parent_directory(path)
    finally:
        _safe_remove_temporary(temporary)


def _write_atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    """Replace one derived bounded checkpoint without changing immutable events."""

    payload = canonical_bytes(dict(document)) + b"\n"
    if len(payload) > MAX_SPOOL_DOCUMENT_BYTES:
        raise PolicyViolation("Lifecycle spool checkpoint boyut sinirini asti")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _write_new_file(temporary, payload)
        if os.path.lexists(path):
            _safe_regular_file_exists(path)
        os.replace(_absolute_no_resolve(temporary), _absolute_no_resolve(path))
        _safe_regular_file_exists(path)
        _fsync_parent_directory(path)
    finally:
        _safe_remove_temporary(temporary)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    _ensure_safe_directory(path.parent)
    _safe_regular_file_exists(path, max_bytes=MAX_LOCK_BYTES)
    flags = os.O_RDWR | os.O_CREAT | _OPEN_BINARY | _OPEN_NOFOLLOW
    try:
        fd = os.open(_absolute_no_resolve(path), flags, 0o600)
    except OSError as exc:
        raise ValidationFailed("Lifecycle spool lock acilamadi") from exc
    with os.fdopen(fd, "r+b", closefd=True) as stream:
        _assert_fd_matches_path(stream.fileno(), path, max_bytes=MAX_LOCK_BYTES)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
            os.fsync(stream.fileno())
            _fsync_parent_directory(path)
        elif stream.tell() != 1:
            raise PolicyViolation("Lifecycle spool lock boyutu gecersiz")
        acquired = False
        try:
            for _ in range(100):
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    time.sleep(0.01)
                    continue
                acquired = True
                break
            if not acquired:
                raise ConcurrencyConflict("Lifecycle spool writer lock alinmadi")
            yield
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
