"""Authority-free, encrypted diagnostic trace contracts."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class TraceVisibility(StrEnum):
    MODEL_VISIBLE = "model-visible"
    RUNTIME_ONLY = "runtime-only"
    DIAGNOSTIC_ONLY = "diagnostic-only"


class TraceEventType(StrEnum):
    SESSION_STARTED = "session-started"
    SESSION_STOPPED = "session-stopped"
    TURN_STARTED = "turn-started"
    TURN_COMPLETED = "turn-completed"
    USER_INPUT = "user-input"
    CONTEXT_FRAGMENT_SELECTED = "context-fragment-selected"
    CONTEXT_SERIALIZED = "context-serialized"
    MODEL_REQUEST_PREPARED = "model-request-prepared"
    MODEL_REQUEST = "model-request"
    MODEL_RESPONSE = "model-response"
    TOOL_REQUEST = "tool-request"
    TOOL_RESULT = "tool-result"
    TERMINAL_OUTPUT = "terminal-output"
    AGENT_SPAWN = "agent-spawn"
    AGENT_TASK = "agent-task"
    AGENT_RESULT = "agent-result"
    AGENT_CLOSE = "agent-close"
    COMPACTION_REQUESTED = "compaction-requested"
    COMPACTION_INSTALLED = "compaction-installed"
    CHECKPOINT_CREATED = "checkpoint-created"
    ENVIRONMENT_PROBED = "environment-probed"
    ENVIRONMENT_DRIFTED = "environment-drifted"
    RUNTIME_STATE = "runtime-state"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DiagnosticTracePolicy:
    enabled: bool
    retention_days: int = 7
    max_payload_bytes: int = 1_048_576
    max_events: int = 10_000
    max_total_bytes: int = 64 * 1_048_576
    encryption_key_ref: str | None = None
    export_allowed: bool = False
    redaction_profile: str = "strict-v1"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.retention_days <= 30:
            raise ValidationFailed("Trace retention 1..30 gun olmali")
        if not 1 <= self.max_payload_bytes <= 8_388_608:
            raise ValidationFailed("Trace payload limiti 1..8388608 byte olmali")
        if not 1 <= self.max_events <= 100_000:
            raise ValidationFailed("Trace event kotasi 1..100000 olmali")
        if not self.max_payload_bytes <= self.max_total_bytes <= 512 * 1_048_576:
            raise ValidationFailed("Trace toplam byte kotasi gecersiz")
        if self.enabled and not (self.encryption_key_ref or "").strip():
            raise PolicyViolation("Acik raw trace logical encryption key ref ister")
        if not self.redaction_profile.strip():
            raise ValidationFailed("Trace redaction profile bos olamaz")
        if self.grants_authority:
            raise PolicyViolation("Diagnostic trace authority veremez")

    @property
    def policy_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-diagnostic-trace-policy/v1",
            "enabled": self.enabled,
            "retention_days": self.retention_days,
            "max_payload_bytes": self.max_payload_bytes,
            "max_events": self.max_events,
            "max_total_bytes": self.max_total_bytes,
            "encryption_key_ref": self.encryption_key_ref,
            "export_allowed": self.export_allowed,
            "redaction_profile": self.redaction_profile,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class TraceBundle:
    id: UUID
    realm_id: UUID
    trace_ref: str
    project_id: UUID | None
    work_item_id: UUID | None
    run_id: UUID | None
    root_assignment_id: UUID | None
    root_client_session_id: str
    policy: DiagnosticTracePolicy
    created_at: dt.datetime
    expires_at: dt.datetime
    state: str = "open"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.trace_ref.strip() or not self.root_client_session_id.strip():
            raise ValidationFailed("Trace ref/root client session bos olamaz")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValidationFailed("Trace zamanlari timezone-aware olmali")
        expected = self.created_at + dt.timedelta(days=self.policy.retention_days)
        if self.expires_at != expected:
            raise ValidationFailed("Trace expiry retention policy ile exact eslesmeli")
        if self.state not in {"open", "closed", "expired"}:
            raise ValidationFailed("Trace bundle state gecersiz")
        if self.grants_authority:
            raise PolicyViolation("Trace bundle authority veremez")

    @property
    def manifest_digest(self) -> str:
        return digest(self.manifest_body())

    def manifest_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-diagnostic-trace-manifest/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "trace_ref": self.trace_ref,
            "project_id": None if self.project_id is None else str(self.project_id),
            "work_item_id": None if self.work_item_id is None else str(self.work_item_id),
            "run_id": None if self.run_id is None else str(self.run_id),
            "root_assignment_id": (
                None if self.root_assignment_id is None else str(self.root_assignment_id)
            ),
            "root_client_session_id": self.root_client_session_id,
            "policy_digest": self.policy.policy_digest,
            "created_at": self.created_at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
            "expires_at": self.expires_at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class TraceEventRecord:
    id: UUID
    realm_id: UUID
    bundle_id: UUID
    sequence: int
    event_type: TraceEventType
    visibility: TraceVisibility
    occurred_at: dt.datetime
    correlation: dict[str, str]
    payload_ref: str
    payload_cipher_digest: str
    payload_plain_digest: str
    payload_size_bytes: int
    encryption_key_ref: str
    redaction_digest: str
    previous_event_digest: str | None
    event_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.sequence < 1 or self.occurred_at.tzinfo is None:
            raise ValidationFailed("Trace event sequence/zaman gecersiz")
        if not self.correlation or any(
            not key.strip() or not value.strip() for key, value in self.correlation.items()
        ):
            raise ValidationFailed("Trace event bounded correlation ister")
        if len(self.correlation) > 16:
            raise ValidationFailed("Trace correlation alan sinirini asti")
        for value in (
            self.payload_ref,
            self.payload_cipher_digest,
            self.payload_plain_digest,
            self.redaction_digest,
            self.event_digest,
        ):
            if not _DIGEST.fullmatch(value):
                raise ValidationFailed("Trace digest gecersiz")
        if self.previous_event_digest is not None and not _DIGEST.fullmatch(
            self.previous_event_digest
        ):
            raise ValidationFailed("Trace previous digest gecersiz")
        if self.payload_size_bytes < 1 or not self.encryption_key_ref.strip():
            raise ValidationFailed("Trace payload metadata gecersiz")
        if self.grants_authority:
            raise PolicyViolation("Trace event authority veremez")
        if self.visibility is TraceVisibility.MODEL_VISIBLE and self.event_type not in {
            TraceEventType.MODEL_REQUEST,
            TraceEventType.MODEL_RESPONSE,
        }:
            raise PolicyViolation("Yalniz final provider request/response model-visible olabilir")
        if self.event_digest != digest(self.body()):
            raise ValidationFailed("Trace event digest mismatch")

    def body(self) -> dict[str, Any]:
        return trace_event_body(
            id=self.id,
            realm_id=self.realm_id,
            bundle_id=self.bundle_id,
            sequence=self.sequence,
            event_type=self.event_type,
            visibility=self.visibility,
            occurred_at=self.occurred_at,
            correlation=self.correlation,
            payload_ref=self.payload_ref,
            payload_cipher_digest=self.payload_cipher_digest,
            payload_plain_digest=self.payload_plain_digest,
            payload_size_bytes=self.payload_size_bytes,
            encryption_key_ref=self.encryption_key_ref,
            redaction_digest=self.redaction_digest,
            previous_event_digest=self.previous_event_digest,
        )


def trace_event_body(
    *,
    id: UUID,
    realm_id: UUID,
    bundle_id: UUID,
    sequence: int,
    event_type: TraceEventType,
    visibility: TraceVisibility,
    occurred_at: dt.datetime,
    correlation: dict[str, str],
    payload_ref: str,
    payload_cipher_digest: str,
    payload_plain_digest: str,
    payload_size_bytes: int,
    encryption_key_ref: str,
    redaction_digest: str,
    previous_event_digest: str | None,
) -> dict[str, Any]:
    return {
        "schema": "zekam-diagnostic-trace-event/v1",
        "id": str(id),
        "realm_id": str(realm_id),
        "bundle_id": str(bundle_id),
        "sequence": sequence,
        "event_type": event_type.value,
        "visibility": visibility.value,
        "occurred_at": occurred_at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        "correlation": dict(sorted(correlation.items())),
        "payload_ref": payload_ref,
        "payload_cipher_digest": payload_cipher_digest,
        "payload_plain_digest": payload_plain_digest,
        "payload_size_bytes": payload_size_bytes,
        "encryption_key_ref": encryption_key_ref,
        "redaction_digest": redaction_digest,
        "previous_event_digest": previous_event_digest,
        "grants_authority": False,
    }


@dataclass(frozen=True, slots=True)
class TraceGraphNode:
    node_id: str
    kind: str
    visibility: TraceVisibility
    sequence: int
    payload_digest: str


@dataclass(frozen=True, slots=True)
class TraceGraphEdge:
    source_node_id: str
    target_node_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class ReducedTrace:
    bundle_id: UUID
    event_count: int
    nodes: tuple[TraceGraphNode, ...]
    edges: tuple[TraceGraphEdge, ...]
    first_event_digest: str
    last_event_digest: str
    reduced_at: dt.datetime
    output_digest: str = field(init=False)
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.event_count < 1 or self.event_count != len(self.nodes):
            raise ValidationFailed("Reduced trace event/node sayisi gecersiz")
        if self.reduced_at.tzinfo is None:
            raise ValidationFailed("Reduced trace zamani timezone-aware olmali")
        if self.grants_authority:
            raise PolicyViolation("Reduced trace authority veremez")
        object.__setattr__(self, "output_digest", digest(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-diagnostic-trace-graph/v1",
            "bundle_id": str(self.bundle_id),
            "event_count": self.event_count,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "visibility": node.visibility.value,
                    "sequence": node.sequence,
                    "payload_digest": node.payload_digest,
                }
                for node in self.nodes
            ],
            "edges": [
                {
                    "source_node_id": edge.source_node_id,
                    "target_node_id": edge.target_node_id,
                    "kind": edge.kind,
                }
                for edge in self.edges
            ],
            "first_event_digest": self.first_event_digest,
            "last_event_digest": self.last_event_digest,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class TracePurgeCandidate:
    bundle_id: UUID
    payload_refs: tuple[str, ...]
    expires_at: dt.datetime

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None or any(
            not _DIGEST.fullmatch(item) for item in self.payload_refs
        ):
            raise ValidationFailed("Trace purge candidate metadata gecersiz")
