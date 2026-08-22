"""Istemci adapter sozlesmesi.

Codex, Claude Code, OpenCode ve kurum ici istemciler birer *adapter*'dir. Core
hicbirine baglanmaz. Adapter yalnizca beyan edilen yeteneklerle cagrilir; sonuc
strict JSON envelope'dur ve free-text authoritative degildir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest
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
    }
)

_SENSITIVE = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|token)",
    re.IGNORECASE,
)


class ClientKind(StrEnum):
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    OPENCODE = "opencode"
    INTERNAL = "internal"


class DispatchOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed-out"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ClientDescriptor:
    """Bir istemcinin exact calistirilabilir dosyasi ve beyan ettigi yetenekler."""

    kind: ClientKind
    client_id: str
    executable: str
    capabilities: frozenset[str]
    version: str | None = None

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
        }

    @property
    def descriptor_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    """Adapter'a verilen bounded is birimi. Transcript veya secret tasimaz."""

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
            client_id=request.client_id,
            role=request.role,
            outcome=DispatchOutcome.FAILED,
            exit_code=None,
            payload={},
            failure_category="invalid-payload",
        )
    return DispatchResult(
        client_id=request.client_id,
        role=request.role,
        outcome=outcome,
        exit_code=document.get("exit_code"),
        payload=payload,
        failure_category=document.get("failure_category")
        or (None if outcome is DispatchOutcome.SUCCESS else "unspecified"),
    )
