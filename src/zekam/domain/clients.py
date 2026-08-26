"""Istemci adapter sozlesmesi.

Codex, Claude Code, OpenCode ve kurum ici istemciler birer *adapter*'dir. Core
hicbirine baglanmaz. Adapter yalnizca beyan edilen yeteneklerle cagrilir; sonuc
strict JSON envelope'dur ve free-text authoritative degildir.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

#: Beyan edilebilen yetenekler. Bilinmeyen yetenek cikarim yoluyla eklenmez.
KNOWN_CAPABILITIES = frozenset(
    {
        "chat",
        "code",
        "tool-use",
        "structured-result",
        "parallel-dispatch",
        "cancellation",
        "model-selection",
        "sandbox-write",
        "lifecycle-events-v2",
    }
)
KNOWN_CLIENT_PERMISSIONS = frozenset(
    {
        "filesystem.read",
        "filesystem.write",
        "network.access",
        "process.run",
        "tool.execute",
    }
)

_SENSITIVE = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|token)",
    re.IGNORECASE,
)
_CANONICAL_DISPATCH_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class CanonicalDispatchPermit:
    """Process-local proof that assignment and invocation were persisted first."""

    assignment_id: UUID
    invocation_id: UUID
    _authority: object

    def assert_valid(self, request: DispatchRequest) -> None:
        if self._authority is not _CANONICAL_DISPATCH_AUTHORITY:
            raise PolicyViolation("Canonical dispatch permit gecersiz")
        if (self.assignment_id, self.invocation_id) != (
            request.assignment_id,
            request.invocation_id,
        ):
            raise PolicyViolation("Canonical dispatch permit request ile uyusmuyor")


def _issue_canonical_dispatch_permit(
    assignment_id: UUID, invocation_id: UUID
) -> CanonicalDispatchPermit:
    """Internal factory; only assignment-first orchestration may call this."""

    return CanonicalDispatchPermit(assignment_id, invocation_id, _CANONICAL_DISPATCH_AUTHORITY)


class ClientKind(StrEnum):
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    OPENCODE = "opencode"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ClientCapabilityManifest:
    """Bir istemcinin versioned, authority-free capability kaniti."""

    client_id: str
    kind: ClientKind
    version: str
    capabilities: tuple[str, ...]
    protocol_version: str = "zekam-client-adapter/v1"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.client_id.strip() or not self.version.strip():
            raise ValidationFailed("client capability kimligi ve surumu bos olamaz")
        if self.grants_authority:
            raise PolicyViolation("client capability manifest authority veremez")
        normalized = tuple(sorted(set(self.capabilities)))
        if normalized != self.capabilities or not normalized:
            raise ValidationFailed("client capability listesi sirali, unique ve dolu olmali")
        unknown = set(normalized) - KNOWN_CAPABILITIES
        if unknown:
            raise ValidationFailed("bilinmeyen client capability beyani")

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "client_id": self.client_id,
            "kind": self.kind.value,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "grants_authority": False,
        }

    @property
    def capability_digest(self) -> str:
        return digest(self.as_dict())

    def unsupported(self, required: tuple[str, ...]) -> tuple[str, ...]:
        unknown = set(required) - KNOWN_CAPABILITIES
        if unknown:
            raise ValidationFailed("bilinmeyen required client capability")
        return tuple(sorted(set(required) - set(self.capabilities)))


@dataclass(frozen=True, slots=True)
class ClientPermissionManifest:
    """Client runtime permission profilinin explicit, authority-free kesiti."""

    profile_id: str
    permissions: tuple[str, ...]
    managed: bool
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValidationFailed("client permission profile id bos olamaz")
        normalized = tuple(sorted(set(self.permissions)))
        if normalized != self.permissions:
            raise ValidationFailed("client permission listesi kanonik olmali")
        if set(normalized) - KNOWN_CLIENT_PERMISSIONS:
            raise ValidationFailed("bilinmeyen client permission")
        if self.grants_authority:
            raise PolicyViolation("client permission manifest authority veremez")

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "permissions": list(self.permissions),
            "managed": self.managed,
            "grants_authority": False,
        }

    @property
    def permission_digest(self) -> str:
        return digest(self.as_dict())

    def unsupported(self, required: tuple[str, ...]) -> tuple[str, ...]:
        unknown = set(required) - KNOWN_CLIENT_PERMISSIONS
        if unknown:
            raise ValidationFailed("bilinmeyen required client permission")
        return tuple(sorted(set(required) - set(self.permissions)))


class DispatchOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed-out"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ClientLifecycleEvent:
    """Tum istemciler icin transcript-free lifecycle event envelope'u."""

    client_id: str
    client_kind: ClientKind
    session_id: str
    sequence: int
    previous_digest: str | None
    event_type: str
    payload_digest: str
    occurred_at: dt.datetime
    transcript_included: bool = False
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.client_id.strip() or not self.session_id.strip() or not self.event_type.strip():
            raise ValidationFailed("client lifecycle kimlik ve event type ister")
        if self.sequence < 1 or (self.sequence == 1) != (self.previous_digest is None):
            raise ValidationFailed("client lifecycle sequence/previous zinciri gecersiz")
        if self.previous_digest is not None:
            parse_digest(self.previous_digest)
        parse_digest(self.payload_digest)
        if self.occurred_at.tzinfo is None:
            raise ValidationFailed("client lifecycle zamani timezone-aware olmali")
        if self.transcript_included or self.grants_authority:
            raise PolicyViolation("client lifecycle transcript veya authority tasiyamaz")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-client-lifecycle-event/v1",
            "client_id": self.client_id,
            "client_kind": self.client_kind.value,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "previous_digest": self.previous_digest,
            "event_type": self.event_type,
            "payload_digest": self.payload_digest,
            "occurred_at": self.occurred_at.isoformat(),
            "transcript_included": False,
            "grants_authority": False,
        }

    @property
    def event_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"event_digest": self.event_digest}


@dataclass(frozen=True, slots=True)
class ClientDescriptor:
    """Bir istemcinin exact calistirilabilir dosyasi ve beyan ettigi yetenekler."""

    kind: ClientKind
    client_id: str
    executable: str
    capabilities: frozenset[str]
    version: str | None = None
    permission_manifest: ClientPermissionManifest | None = None

    def __post_init__(self) -> None:
        if not self.client_id.strip():
            raise ValidationFailed("client kimligi bos olamaz")
        if not self.executable.strip():
            raise PolicyViolation("istemci exact calistirilabilir dosya beyan etmeli")
        if _SENSITIVE.search(self.executable):
            raise PolicyViolation("calistirilabilir yol secret benzeri deger tasiyamaz")
        unknown = self.capabilities - KNOWN_CAPABILITIES
        if unknown:
            raise ValidationFailed("bilinmeyen yetenek beyani")
        if not self.capabilities:
            raise ValidationFailed("istemci en az bir yetenek beyan etmeli")

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def assert_supports(self, capability: str) -> None:
        """Yetenek beyan edilmemisse cikarim yapilmaz; islem reddedilir."""

        if capability not in KNOWN_CAPABILITIES:
            raise ValidationFailed("bilinmeyen yetenek sorgusu")
        if not self.supports(capability):
            raise PolicyViolation(f"istemci {capability} yetenegini beyan etmiyor")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "client_id": self.client_id,
            "executable": self.executable,
            "capabilities": sorted(self.capabilities),
            "version": self.version,
            "permission_manifest_digest": (
                None
                if self.permission_manifest is None
                else self.permission_manifest.permission_digest
            ),
        }

    @property
    def descriptor_digest(self) -> str:
        return digest(self.as_dict())

    @property
    def capability_manifest(self) -> ClientCapabilityManifest:
        return ClientCapabilityManifest(
            client_id=self.client_id,
            kind=self.kind,
            version=self.version or "unknown",
            capabilities=tuple(sorted(self.capabilities)),
        )


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    """Adapter'a verilen bounded is birimi. Transcript veya secret tasimaz."""

    assignment_id: UUID
    invocation_id: UUID
    client_id: str
    role: str
    instruction_digest: str
    context_manifest_digest: str
    timeout_seconds: int
    requires_structured_result: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 3600:
            raise ValidationFailed("timeout 1..3600 araliginda olmali")
        for label, value in (
            ("client_id", self.client_id),
            ("role", self.role),
        ):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": str(self.assignment_id),
            "invocation_id": str(self.invocation_id),
            "client_id": self.client_id,
            "role": self.role,
            "instruction_digest": self.instruction_digest,
            "context_manifest_digest": self.context_manifest_digest,
            "timeout_seconds": self.timeout_seconds,
            "requires_structured_result": self.requires_structured_result,
        }


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Strict adapter sonucu.

    `payload` yalnizca makine dogrulanabilir alanlar tasir. Free-text ozet
    authoritative degildir ve `grants_authority` her zaman false'tur.
    """

    assignment_id: UUID
    invocation_id: UUID
    client_id: str
    role: str
    outcome: DispatchOutcome
    exit_code: int | None
    payload: dict[str, Any]
    failure_category: str | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("adapter sonucu authority veremez")
        if self.outcome is DispatchOutcome.SUCCESS and not self.payload:
            raise ValidationFailed("success sonucu bos payload dondurmemeli")
        if self.outcome is not DispatchOutcome.SUCCESS and not self.failure_category:
            raise ValidationFailed("non-success sonuc failure kategorisi ister")
        for key, value in self.payload.items():
            if _SENSITIVE.search(str(key)) or _SENSITIVE.search(str(value)):
                raise PolicyViolation("adapter payload'i secret benzeri deger tasiyamaz")

    @property
    def is_success(self) -> bool:
        return self.outcome is DispatchOutcome.SUCCESS

    def as_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": str(self.assignment_id),
            "invocation_id": str(self.invocation_id),
            "client_id": self.client_id,
            "role": self.role,
            "outcome": str(self.outcome),
            "exit_code": self.exit_code,
            "payload": self.payload,
            "failure_category": self.failure_category,
            "grants_authority": False,
        }

    @property
    def result_digest(self) -> str:
        return digest(self.as_dict())


def parse_result(
    descriptor: ClientDescriptor,
    request: DispatchRequest,
    document: Any,
) -> DispatchResult:
    """Adapter ciktisini strict envelope'a cevirir.

    Sema disi cikti sessizce kabul edilmez; ayristirilamayan sonuc `failed`
    olarak siniflanir ve gorunur kalir.
    """

    if descriptor.client_id != request.client_id:
        raise ValidationFailed("istek baska bir istemciye ait")
    if not isinstance(document, dict):
        return DispatchResult(
            assignment_id=request.assignment_id,
            invocation_id=request.invocation_id,
            client_id=request.client_id,
            role=request.role,
            outcome=DispatchOutcome.FAILED,
            exit_code=None,
            payload={},
            failure_category="unparsable-result",
        )
    raw_outcome = document.get("outcome")
    try:
        outcome = DispatchOutcome(str(raw_outcome))
    except ValueError:
        return DispatchResult(
            assignment_id=request.assignment_id,
            invocation_id=request.invocation_id,
            client_id=request.client_id,
            role=request.role,
            outcome=DispatchOutcome.FAILED,
            exit_code=None,
            payload={},
            failure_category="unknown-outcome",
        )
    payload = document.get("payload", {})
    if not isinstance(payload, dict):
        return DispatchResult(
            assignment_id=request.assignment_id,
            invocation_id=request.invocation_id,
            client_id=request.client_id,
            role=request.role,
            outcome=DispatchOutcome.FAILED,
            exit_code=None,
            payload={},
            failure_category="invalid-payload",
        )
    return DispatchResult(
        assignment_id=request.assignment_id,
        invocation_id=request.invocation_id,
        client_id=request.client_id,
        role=request.role,
        outcome=outcome,
        exit_code=document.get("exit_code"),
        payload=payload,
        failure_category=document.get("failure_category")
        or (None if outcome is DispatchOutcome.SUCCESS else "unspecified"),
    )
