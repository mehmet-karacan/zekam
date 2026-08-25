"""Encrypted diagnostic trace writer and deterministic offline reducer."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from zekam.application.object_store import ObjectStore
from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.diagnostic_trace import (
    DiagnosticTracePolicy,
    ReducedTrace,
    TraceBundle,
    TraceEventRecord,
    TraceEventType,
    TraceGraphEdge,
    TraceGraphNode,
    TracePurgeCandidate,
    TraceVisibility,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed

_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|credential|authorization|cookie|"
    r"chain[_-]?of[_-]?thought|private[_-]?reasoning|reasoning_content)"
)
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:\\Users\\[^\\\s]+|/(?:home|Users)/[^/\s]+)")
_PII = re.compile(r"(?i)(?:\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\b\+?\d[\d ()-]{8,}\d\b)")


def _bytes_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def decode_trace_key(raw: str) -> bytes:
    value = raw.strip()
    try:
        decoded = (
            bytes.fromhex(value) if len(value) == 64 else base64.b64decode(value, validate=True)
        )
    except (ValueError, binascii.Error) as exc:
        raise ValidationFailed("Trace encryption key 32-byte hex/base64 olmali") from exc
    if len(decoded) != 32:
        raise ValidationFailed("Trace encryption key tam 32 byte olmali")
    return decoded


class TraceCipher(Protocol):
    def encrypt(self, plaintext: bytes, *, key: bytes, aad: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes, *, key: bytes, aad: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class AesGcmTraceCipher:
    """AES-256-GCM envelope; nonce ciphertext'in basinda saklanir."""

    nonce_factory: Callable[[int], bytes]

    def encrypt(self, plaintext: bytes, *, key: bytes, aad: bytes) -> bytes:
        if len(key) != 32:
            raise PolicyViolation("Trace AES-256 key tam 32 byte olmali")
        nonce = self.nonce_factory(12)
        if len(nonce) != 12:
            raise ValidationFailed("Trace AES-GCM nonce tam 12 byte olmali")
        return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)

    def decrypt(self, ciphertext: bytes, *, key: bytes, aad: bytes) -> bytes:
        if len(key) != 32 or len(ciphertext) < 29:
            raise PolicyViolation("Trace ciphertext/key gecersiz")
        return AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], aad)


@dataclass(frozen=True, slots=True)
class TraceEventMetadata:
    id: UUID
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
    durability_receipt: dict[str, Any]
    durability_receipt_digest: str


class TraceRepository(Protocol):
    def create_bundle(self, bundle: TraceBundle) -> tuple[UUID, bool]: ...

    def append_event(
        self, bundle: TraceBundle, metadata: TraceEventMetadata
    ) -> TraceEventRecord: ...

    def list_events(
        self, bundle_id: UUID, *, authorization_ref: str | None = None
    ) -> tuple[TraceEventRecord, ...]: ...

    def store_reduction(
        self, reduced: ReducedTrace, *, authorization_ref: str | None = None
    ) -> tuple[UUID, bool]: ...

    def usage(self, bundle_id: UUID) -> tuple[int, int]: ...

    def expired_candidates(
        self, *, now: dt.datetime, limit: int
    ) -> tuple[TracePurgeCandidate, ...]: ...

    def mark_purged(
        self,
        bundle_id: UUID,
        *,
        purged_at: dt.datetime,
        purge_receipt_digest: str,
        authorization_ref: str,
    ) -> None: ...


class RuntimeTraceSink(Protocol):
    """Production runtime'larinin paylastigi best-effort root/child trace portu."""

    def record(
        self,
        *,
        event_type: TraceEventType,
        visibility: TraceVisibility,
        payload: Any,
        correlation: dict[str, str],
        occurred_at: dt.datetime,
    ) -> TraceWriteResult: ...


@dataclass(frozen=True, slots=True)
class TraceWriteResult:
    state: str
    bundle_id: UUID | None
    event_id: UUID | None
    event_digest: str | None
    error_category: str | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.state not in {"disabled", "recorded", "failed"}:
            raise ValidationFailed("Trace write state gecersiz")
        if self.grants_authority:
            raise PolicyViolation("Trace write authority veremez")


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            sanitized[text_key] = (
                "[REDACTED]" if _SENSITIVE_KEY.search(text_key) else _sanitize(item)
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        findings = scan_text(value, relative_path="trace-payload", max_lines=20_000)
        return (
            "[REDACTED]"
            if findings or _ABSOLUTE_PATH.search(value) or _PII.search(value)
            else value
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValidationFailed("Trace payload yalniz JSON-compatible deger kabul eder")


@dataclass(slots=True)
class DiagnosticTraceWriter:
    repository: TraceRepository
    object_store: ObjectStore
    cipher: TraceCipher
    key_resolver: Callable[[str], bytes]

    def open_bundle(
        self,
        *,
        realm_id: UUID,
        trace_ref: str,
        policy: DiagnosticTracePolicy,
        project_id: UUID | None,
        work_item_id: UUID | None,
        run_id: UUID | None,
        root_assignment_id: UUID | None,
        root_client_session_id: str,
        now: dt.datetime,
    ) -> TraceBundle | None:
        if not policy.enabled:
            return None
        bundle = TraceBundle(
            id=uuid4(),
            realm_id=realm_id,
            trace_ref=trace_ref,
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            root_assignment_id=root_assignment_id,
            root_client_session_id=root_client_session_id,
            policy=policy,
            created_at=now,
            expires_at=now + dt.timedelta(days=policy.retention_days),
        )
        self.repository.create_bundle(bundle)
        return bundle

    def write(
        self,
        *,
        bundle: TraceBundle | None,
        policy: DiagnosticTracePolicy,
        event_type: TraceEventType,
        visibility: TraceVisibility,
        payload: Any,
        correlation: dict[str, str],
        occurred_at: dt.datetime,
    ) -> TraceWriteResult:
        if not policy.enabled:
            return TraceWriteResult("disabled", None, None, None)
        if bundle is None or bundle.policy.policy_digest != policy.policy_digest:
            raise PolicyViolation("Trace bundle/policy exact binding mismatch")
        if bundle.state != "open" or occurred_at > bundle.expires_at:
            raise PolicyViolation("Trace bundle open/current degil")
        sanitized = _sanitize(payload)
        plaintext = canonical_bytes(sanitized)
        if len(plaintext) > policy.max_payload_bytes:
            raise ValidationFailed("Trace payload bounded limiti asti")
        event_count, total_bytes = self.repository.usage(bundle.id)
        if (
            event_count >= policy.max_events
            or total_bytes + len(plaintext) > policy.max_total_bytes
        ):
            raise PolicyViolation("Trace event/volume kotasi doldu")
        event_id = uuid4()
        aad = f"{bundle.id}:{event_id}".encode()
        key_ref = policy.encryption_key_ref or ""
        ciphertext = self.cipher.encrypt(plaintext, key=self.key_resolver(key_ref), aad=aad)
        # Durability-before-reference: DB event yazimindan once CAS put tamamlanir.
        stored = self.object_store.put(
            ciphertext,
            media_type="application/vnd.zekam.trace+ciphertext",
            metadata={"purpose": "diagnostic-trace", "cipher": "aes-256-gcm"},
        )
        if not self.object_store.exists(stored.digest):
            raise PolicyViolation("Trace payload durable object receipt uretemedi")
        verified = self.object_store.stat(stored.digest)
        if verified != stored or self.object_store.get(stored.digest) != ciphertext:
            raise PolicyViolation("Trace payload CAS receipt/durable bytes dogrulanamadi")
        durability_receipt = {
            "schema": "zekam-trace-payload-durability-receipt/v2",
            "trace_id": str(bundle.id),
            "event_id": str(event_id),
            "object": verified.as_dict(),
            "durable_before_event": True,
        }
        metadata = TraceEventMetadata(
            id=event_id,
            event_type=event_type,
            visibility=visibility,
            occurred_at=occurred_at,
            correlation=dict(sorted(correlation.items())),
            payload_ref=stored.digest,
            payload_cipher_digest=stored.digest,
            payload_plain_digest=_bytes_digest(plaintext),
            payload_size_bytes=len(plaintext),
            encryption_key_ref=key_ref,
            redaction_digest=digest(
                {"profile": policy.redaction_profile, "sanitized_payload": sanitized}
            ),
            durability_receipt=durability_receipt,
            durability_receipt_digest=digest(durability_receipt),
        )
        event = self.repository.append_event(bundle, metadata)
        return TraceWriteResult("recorded", bundle.id, event.id, event.event_digest)

    def write_best_effort(self, **kwargs: Any) -> TraceWriteResult:
        """Instrumentation boundary: trace failure canonical flow'a yayilmaz."""
        try:
            return self.write(**kwargs)
        except Exception as exc:
            bundle = kwargs.get("bundle")
            return TraceWriteResult(
                "failed",
                None if bundle is None else bundle.id,
                None,
                None,
                type(exc).__name__,
            )


@dataclass(frozen=True, slots=True)
class BoundDiagnosticTraceSink:
    """Tek bundle/writer'i root ve child runtime producer'lari arasinda paylastirir."""

    writer: DiagnosticTraceWriter
    bundle: TraceBundle
    policy: DiagnosticTracePolicy

    def record(
        self,
        *,
        event_type: TraceEventType,
        visibility: TraceVisibility,
        payload: Any,
        correlation: dict[str, str],
        occurred_at: dt.datetime,
    ) -> TraceWriteResult:
        return self.writer.write_best_effort(
            bundle=self.bundle,
            policy=self.policy,
            event_type=event_type,
            visibility=visibility,
            payload=payload,
            correlation=correlation,
            occurred_at=occurred_at,
        )


@dataclass(frozen=True, slots=True)
class TraceReplayInput:
    bundle_id: UUID
    events: tuple[TraceEventRecord, ...]
    ciphertexts: tuple[bytes, ...] = field(repr=False)


@dataclass(slots=True)
class DiagnosticTraceReducer:
    repository: TraceRepository
    object_store: ObjectStore
    cipher: TraceCipher
    key_resolver: Callable[[str], bytes]

    def preflight(
        self,
        bundle: TraceBundle,
        *,
        authorization_ref: str | None = None,
    ) -> TraceReplayInput:
        if bundle.state != "closed":
            raise PolicyViolation("Offline trace reduction closed bundle ister")
        events = self.repository.list_events(bundle.id, authorization_ref=authorization_ref)
        if not events:
            raise ValidationFailed("Bos trace bundle reduce edilemez")
        ciphertexts: list[bytes] = []
        prior: TraceEventRecord | None = None
        for expected_sequence, event in enumerate(events, start=1):
            if event.sequence != expected_sequence:
                raise ValidationFailed("Trace event sequence gap/reorder")
            if event.previous_event_digest != (None if prior is None else prior.event_digest):
                raise ValidationFailed("Trace event hash chain mismatch")
            if not self.object_store.exists(event.payload_ref):
                raise ValidationFailed("Trace payload object missing")
            ciphertext = self.object_store.get(event.payload_ref)
            if _bytes_digest(ciphertext) != event.payload_cipher_digest:
                raise ValidationFailed("Trace ciphertext digest mismatch")
            ciphertexts.append(ciphertext)
            prior = event
        return TraceReplayInput(bundle.id, events, tuple(ciphertexts))

    def reduce(
        self,
        bundle: TraceBundle,
        *,
        reduced_at: dt.datetime,
        authorization_ref: str | None = None,
        prepared: TraceReplayInput | None = None,
    ) -> ReducedTrace:
        replay = prepared or self.preflight(bundle, authorization_ref=authorization_ref)
        if replay.bundle_id != bundle.id:
            raise PolicyViolation("Trace preflight bundle binding mismatch")
        events = replay.events
        nodes: list[TraceGraphNode] = []
        edges: list[TraceGraphEdge] = []
        known: set[str] = set()
        prior: TraceEventRecord | None = None
        for event, ciphertext in zip(events, replay.ciphertexts, strict=True):
            aad = f"{bundle.id}:{event.id}".encode()
            try:
                plaintext = self.cipher.decrypt(
                    ciphertext,
                    key=self.key_resolver(event.encryption_key_ref),
                    aad=aad,
                )
            except (InvalidTag, ValueError) as exc:
                raise ValidationFailed("Trace ciphertext authentication gecersiz") from exc
            try:
                decoded = json.loads(plaintext)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationFailed("Trace decrypted payload canonical JSON degil") from exc
            if canonical_bytes(decoded) != plaintext:
                raise ValidationFailed("Trace decrypted payload canonical byte drift")
            node_id = f"event:{event.id}"
            node_kind = _reduced_node_kind(event.event_type, event.visibility)
            nodes.append(
                TraceGraphNode(
                    node_id=node_id,
                    kind=node_kind,
                    visibility=event.visibility,
                    sequence=event.sequence,
                    payload_digest=event.payload_plain_digest,
                )
            )
            if prior is not None:
                edges.append(TraceGraphEdge(f"event:{prior.id}", node_id, "next"))
            parent = event.correlation.get("parent_event_id")
            if parent is not None:
                parent_node = f"event:{parent}"
                if parent_node not in known:
                    raise ValidationFailed("Trace parent event missing/reordered")
                edges.append(TraceGraphEdge(parent_node, node_id, "caused"))
            known.add(node_id)
            prior = event
        reduced = ReducedTrace(
            bundle_id=bundle.id,
            event_count=len(events),
            nodes=tuple(nodes),
            edges=tuple(edges),
            first_event_digest=events[0].event_digest,
            last_event_digest=events[-1].event_digest,
            reduced_at=reduced_at,
        )
        self.repository.store_reduction(reduced, authorization_ref=authorization_ref)
        return reduced


@dataclass(frozen=True, slots=True)
class TracePurgeResult:
    purged_trace_ids: tuple[UUID, ...]
    deleted_payload_count: int
    missing_payload_count: int
    grants_authority: bool = False


@dataclass(frozen=True, slots=True)
class DiagnosticTraceRetentionService:
    repository: TraceRepository
    object_store: ObjectStore

    def purge_expired(
        self,
        *,
        now: dt.datetime,
        authorization_ref: str,
        limit: int = 100,
    ) -> TracePurgeResult:
        if now.tzinfo is None or not authorization_ref.strip() or not 1 <= limit <= 1000:
            raise ValidationFailed("Trace purge zaman/authorization/limit gecersiz")
        purged: list[UUID] = []
        deleted = 0
        missing = 0
        for candidate in self.repository.expired_candidates(now=now, limit=limit):
            if candidate.expires_at > now:
                raise PolicyViolation("Current trace purge adayi olamaz")
            outcomes: list[dict[str, str]] = []
            for payload_ref in candidate.payload_refs:
                removed = self.object_store.delete(payload_ref)
                deleted += int(removed)
                missing += int(not removed)
                outcomes.append(
                    {"payload_ref": payload_ref, "outcome": "deleted" if removed else "missing"}
                )
            receipt = digest(
                {
                    "schema": "zekam-trace-purge-receipt/v1",
                    "trace_id": str(candidate.bundle_id),
                    "authorization_ref": authorization_ref,
                    "purged_at": now,
                    "payloads": outcomes,
                }
            )
            self.repository.mark_purged(
                candidate.bundle_id,
                purged_at=now,
                purge_receipt_digest=receipt,
                authorization_ref=authorization_ref,
            )
            purged.append(candidate.bundle_id)
        return TracePurgeResult(tuple(purged), deleted, missing)


def export_trace_graph(
    reduced: ReducedTrace,
    *,
    policy: DiagnosticTracePolicy,
    authorization_ref: str | None,
) -> bytes:
    if not policy.export_allowed or not (authorization_ref or "").strip():
        raise PolicyViolation("Trace export policy ve exact authorization ister")
    document = reduced.body() | {"output_digest": reduced.output_digest}
    return canonical_bytes(document)


def replay_digest(events: Sequence[TraceEventRecord]) -> str:
    """Raw payloadsiz metadata replay fixture digest'i."""
    return digest([event.body() | {"event_digest": event.event_digest} for event in events])


def _reduced_node_kind(event_type: TraceEventType, visibility: TraceVisibility) -> str:
    if visibility is TraceVisibility.MODEL_VISIBLE:
        return "ConversationItem"
    if event_type in {
        TraceEventType.MODEL_REQUEST_PREPARED,
        TraceEventType.MODEL_REQUEST,
        TraceEventType.MODEL_RESPONSE,
    }:
        return "InferenceCall"
    if event_type in {TraceEventType.TOOL_REQUEST, TraceEventType.TOOL_RESULT}:
        return "ToolCall"
    if event_type is TraceEventType.TERMINAL_OUTPUT:
        return "TerminalOperation"
    if event_type in {
        TraceEventType.AGENT_SPAWN,
        TraceEventType.AGENT_TASK,
        TraceEventType.AGENT_RESULT,
        TraceEventType.AGENT_CLOSE,
    }:
        return "AgentThread"
    if event_type in {
        TraceEventType.COMPACTION_REQUESTED,
        TraceEventType.COMPACTION_INSTALLED,
    }:
        return "Compaction"
    if event_type in {
        TraceEventType.ENVIRONMENT_PROBED,
        TraceEventType.ENVIRONMENT_DRIFTED,
    }:
        return "EnvironmentSnapshot"
    return "RawPayloadRef"
