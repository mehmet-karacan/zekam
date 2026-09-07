"""Sanitized evolution-capture projection and bounded source-replay decisions.

This module reuses the canonical lifecycle plan and receipt identities.  It does
not persist raw prompt/response/transcript content and does not grant authority.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from zekam.application.client_lifecycle_bridge import (
    LifecycleBridgePlan,
    LifecycleClientContract,
)
from zekam.application.client_lifecycle_spool import (
    CanonicalLifecycleReceipt,
    ClientLifecycleSpool,
    LifecycleSpoolEntry,
)
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_REPLAY_EVENTS = 256
_SAFE_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_GIT_REVISION = re.compile(r"^(?:git:)?[0-9a-f]{7,64}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")


def _safe_ref(value: str, label: str) -> str:
    """Reject path-like, assignment-like and unbounded values at persistence boundary."""

    if not isinstance(value, str) or _SAFE_REF.fullmatch(value) is None:
        raise ValidationFailed(f"{label} path/secret tasimayan canonical ref olmali")
    return value


def _utc(value: dt.datetime, label: str) -> str:
    if value.tzinfo is None:
        raise ValidationFailed(f"{label} timezone-aware olmali")
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class EvolutionCaptureEnvelope:
    """A content-free field-map view over one canonical lifecycle plan."""

    body: Mapping[str, Any]
    capture_digest: str

    def __post_init__(self) -> None:
        frozen = MappingProxyType(dict(self.body))
        if digest(dict(frozen)) != self.capture_digest:
            raise PolicyViolation("Capture envelope digest mismatch")
        object.__setattr__(self, "body", frozen)

    @classmethod
    def from_terminal(
        cls,
        plan: LifecycleBridgePlan,
        contract: LifecycleClientContract,
        *,
        spool: ClientLifecycleSpool,
        entry: LifecycleSpoolEntry,
        receipt: CanonicalLifecycleReceipt,
    ) -> EvolutionCaptureEnvelope:
        """Derive the projection only from verified production identities."""

        event = plan.event
        client_version = contract.installed_version
        receipt.assert_binding(entry)
        binding = receipt.continuity_binding
        if (
            contract.contract_digest != plan.client_contract_digest
            or contract.descriptor.client_id != event.client_id
            or not isinstance(client_version, str)
            or binding is None
            or entry.client_id != event.client_id
            or entry.session_id != event.session_id
            or entry.sequence != event.sequence
            or entry.observation_digest != event.payload_digest
            or plan.idempotency_key != entry.delivery_id
            or event.origin != f"client:{entry.client_id}"
            or _GIT_REVISION.fullmatch(event.source_revision) is None
            or _SEMVER.fullmatch(client_version) is None
        ):
            raise PolicyViolation("Capture contract/plan/spool/receipt binding drift")
        device_id = spool.client_instance_id()
        if device_id != plan.event.client_id and not device_id.startswith(f"{entry.client_id}-"):
            raise PolicyViolation("Capture trusted client instance binding drift")
        binding_digest = str(binding["binding_digest"])
        evidence_refs = (
            f"canonical-event:{receipt.canonical_event_digest}",
            f"canonical-lookup:{receipt.canonical_lookup_digest}",
            f"continuity-binding:{binding_digest}",
        )
        body: dict[str, Any] = {
            "schema": "zekam-evolution-capture-envelope/v1",
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "schema_version": 1,
            "device_id": _safe_ref(device_id, "Capture device id"),
            "client_id": _safe_ref(event.client_id, "Capture client id"),
            "client_version": _safe_ref(client_version, "Capture client version"),
            "session_id": _safe_ref(event.session_id, "Capture session id"),
            "project_scope": f"project:{event.project_id}",
            "work_ref": f"work:{event.work_item_id}",
            "run_ref": f"run:{event.run_id}",
            "source_revision": _safe_ref(event.source_revision, "Capture source revision"),
            "occurred_at": _utc(event.occurred_at, "Capture occurred_at"),
            "received_at": _utc(event.ingested_at, "Capture received_at"),
            "sequence_or_cursor": event.sequence,
            "idempotency_key": entry.delivery_id,
            "parent_run_ref": None,
            "origin": _safe_ref(event.origin, "Capture origin"),
            "payload_digest": event.payload_digest,
            "privacy_class": event.classification.value,
            "evidence_refs": evidence_refs,
            "contains_prompt": False,
            "contains_response": False,
            "contains_transcript": False,
            "grants_authority": False,
        }
        return cls(body, digest(body))

    def as_dict(self) -> dict[str, Any]:
        return dict(self.body) | {"capture_digest": self.capture_digest}


@dataclass(frozen=True, slots=True)
class EvolutionCaptureReceipt:
    capture: Mapping[str, Any]
    capture_digest: str
    spool_entry_digest: str
    canonical_event_digest: str
    canonical_lookup_digest: str
    continuity_binding_digest: str

    def __post_init__(self) -> None:
        frozen = MappingProxyType(dict(self.capture))
        if digest(dict(frozen)) != self.capture_digest:
            raise PolicyViolation("Capture receipt envelope digest mismatch")
        object.__setattr__(self, "capture", frozen)

    @classmethod
    def verified(
        cls,
        envelope: EvolutionCaptureEnvelope,
        entry: LifecycleSpoolEntry,
        receipt: CanonicalLifecycleReceipt,
    ) -> EvolutionCaptureReceipt:
        receipt.assert_binding(entry)
        binding = receipt.continuity_binding
        if binding is None:
            raise PolicyViolation("Capture receipt continuity binding eksik")
        if (
            envelope.body["session_id"] != entry.session_id
            or envelope.body["client_id"] != entry.client_id
            or envelope.body["sequence_or_cursor"] != entry.sequence
            or envelope.body["payload_digest"] != entry.observation_digest
            or envelope.body["event_type"] != entry.internal_event_type
        ):
            raise PolicyViolation("Capture envelope/spool binding drift")
        binding_digest = str(binding.get("binding_digest", ""))
        parse_digest(binding_digest)
        return cls(
            envelope.body,
            envelope.capture_digest,
            entry.entry_digest,
            receipt.canonical_event_digest,
            receipt.canonical_lookup_digest,
            binding_digest,
        )

    def as_dict(self) -> dict[str, Any]:
        body = {
            "schema": "zekam-evolution-capture-receipt/v2",
            "capture": dict(self.capture) | {"capture_digest": self.capture_digest},
            "capture_digest": self.capture_digest,
            "spool_entry_digest": self.spool_entry_digest,
            "canonical_event_digest": self.canonical_event_digest,
            "canonical_lookup_digest": self.canonical_lookup_digest,
            "continuity_binding_digest": self.continuity_binding_digest,
            "status": "completed",
            "grants_authority": False,
        }
        return body | {"receipt_digest": digest(body)}


@dataclass(frozen=True, slots=True)
class CaptureReplayDecision:
    state: Literal["replay", "up-to-date", "capture-gap"]
    source_ref: str
    source_digest: str | None
    after_sequence: int
    through_sequence: int
    limit: int
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        body = {
            "schema": "zekam-capture-replay-decision/v1",
            "state": self.state,
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "after_sequence": self.after_sequence,
            "through_sequence": self.through_sequence,
            "limit": self.limit,
            "reason": self.reason,
            "inferred_content": False,
            "grants_authority": False,
        }
        return body | {"decision_digest": digest(body)}


def inspect_capture_replay(
    *,
    spool: ClientLifecycleSpool,
    client_id: str,
    session_id: str,
    last_delivered_sequence: int,
) -> CaptureReplayDecision:
    """Read the approved bounded spool source; callers cannot assert source authority."""

    if last_delivered_sequence < 0:
        raise ValidationFailed("Capture replay sequence negatif olamaz")
    source_ref = f"client-spool:{client_id}:{digest(session_id)}"
    try:
        window = spool.read_session_window(
            client_id=client_id,
            session_id=session_id,
            after_sequence=last_delivered_sequence,
            limit=MAX_REPLAY_EVENTS,
        )
    except (OSError, PolicyViolation):
        return CaptureReplayDecision(
            "capture-gap",
            source_ref,
            None,
            last_delivered_sequence,
            last_delivered_sequence,
            0,
            "source-unavailable",
        )
    if window is None:
        return CaptureReplayDecision(
            "capture-gap",
            source_ref,
            None,
            last_delivered_sequence,
            last_delivered_sequence,
            0,
            "source-window-expired",
        )
    last_source_sequence, entries = window
    source_digest = digest(
        {
            "schema": "zekam-capture-source-window/v1",
            "client_id": client_id,
            "session_id_digest": digest(session_id),
            "entries": [entry.entry_digest for entry in entries],
        }
    )
    if entries and tuple(entry.sequence for entry in entries) != tuple(
        range(last_delivered_sequence + 1, last_source_sequence + 1)
    ):
        raise PolicyViolation("Capture replay source sequence zinciri gecersiz")
    if last_delivered_sequence > last_source_sequence:
        return CaptureReplayDecision(
            "capture-gap",
            source_ref,
            source_digest,
            last_delivered_sequence,
            last_source_sequence,
            0,
            "source-window-expired",
        )
    if last_source_sequence <= last_delivered_sequence:
        return CaptureReplayDecision(
            "up-to-date",
            source_ref,
            source_digest,
            last_delivered_sequence,
            last_source_sequence,
            0,
            None,
        )
    return CaptureReplayDecision(
        "replay",
        source_ref,
        source_digest,
        last_delivered_sequence,
        min(last_source_sequence, last_delivered_sequence + MAX_REPLAY_EVENTS),
        min(last_source_sequence - last_delivered_sequence, MAX_REPLAY_EVENTS),
        None,
    )


def persist_capture_gap(
    *, spool: ClientLifecycleSpool, decision: CaptureReplayDecision
) -> dict[str, Any]:
    """Persist only a verified gap decision in its exact approved spool."""

    if decision.state != "capture-gap":
        raise PolicyViolation("Yalniz capture-gap karari kalici kaydedilebilir")
    return spool.record_capture_gap(decision.as_dict())
