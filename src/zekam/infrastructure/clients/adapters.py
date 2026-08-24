"""Codex, Claude Code ve OpenCode istemci adapter'lari.

Adapter'lar core'a bagimlilik eklemez; core adapter'a baglanmaz. Her adapter
exact calistirilabilir dosya, beyan edilmis yetenek ve strict JSON sonuc ile
calisir. Beyan edilmeyen yetenek cikarim yoluyla varsayilmaz.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from zekam.domain.clients import (
    CanonicalDispatchPermit,
    ClientCapabilityManifest,
    ClientDescriptor,
    ClientKind,
    ClientLifecycleEvent,
    ClientPermissionManifest,
    DispatchOutcome,
    DispatchRequest,
    DispatchResult,
    parse_result,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.sandbox import ProcessSpec
from zekam.infrastructure.process import runner


class ClientLifecycleAdapter(Protocol):
    """Istemci kimligi, capability'si, lifecycle'i ve dispatch ortak sozlesmesi."""

    @property
    def descriptor(self) -> ClientDescriptor: ...

    @property
    def capability_manifest(self) -> ClientCapabilityManifest: ...

    def lifecycle_event(
        self,
        *,
        session_id: str,
        sequence: int,
        previous_digest: str | None,
        event_type: str,
        payload_digest: str,
        occurred_at: dt.datetime,
    ) -> ClientLifecycleEvent: ...

    def dispatch(
        self, request: DispatchRequest, *, cwd: Path, permit: CanonicalDispatchPermit
    ) -> DispatchResult: ...


ClientAdapter = ClientLifecycleAdapter


@dataclass(frozen=True, slots=True)
class SubprocessClientAdapter:
    """Alt surec cagiran genel adapter.

    Komut satiri `--role`, `--instruction-digest` ve `--context-digest`
    argumanlariyla kurulur; talimat metni veya secret gecirilmez. Sonuc
    stdout'tan strict JSON olarak okunur.
    """

    descriptor: ClientDescriptor
    result_flag: str = "--json"
    #: Yorumlayici gerektiren istemciler icin on ek (ornegin python).
    launcher: tuple[str, ...] = ()
    #: Adapter'a ozel, secret icermeyen ortam degiskenleri.
    env: tuple[tuple[str, str], ...] = ()

    @property
    def capability_manifest(self) -> ClientCapabilityManifest:
        return self.descriptor.capability_manifest

    def lifecycle_event(
        self,
        *,
        session_id: str,
        sequence: int,
        previous_digest: str | None,
        event_type: str,
        payload_digest: str,
        occurred_at: dt.datetime,
    ) -> ClientLifecycleEvent:
        return ClientLifecycleEvent(
            client_id=self.descriptor.client_id,
            client_kind=self.descriptor.kind,
            session_id=session_id,
            sequence=sequence,
            previous_digest=previous_digest,
            event_type=event_type,
            payload_digest=payload_digest,
            occurred_at=occurred_at,
        )

    def build_spec(self, request: DispatchRequest) -> ProcessSpec:
        return ProcessSpec(
            argv=(
                *self.launcher,
                self.descriptor.executable,
                "--assignment-id",
                str(request.assignment_id),
                "--invocation-id",
                str(request.invocation_id),
                "--role",
                request.role,
                "--instruction-digest",
                request.instruction_digest,
                "--context-digest",
                request.context_manifest_digest,
                self.result_flag,
            ),
            timeout_seconds=request.timeout_seconds,
            env=self.env,
        )

    def dispatch(
        self, request: DispatchRequest, *, cwd: Path, permit: CanonicalDispatchPermit
    ) -> DispatchResult:
        permit.assert_valid(request)
        if request.requires_structured_result:
            self.descriptor.assert_supports("structured-result")
        spec = self.build_spec(request)
        output = runner.run(spec, cwd=cwd)
        if output.result.timed_out:
            self._assert_cancellable()
            return DispatchResult(
                assignment_id=request.assignment_id,
                invocation_id=request.invocation_id,
                client_id=request.client_id,
                role=request.role,
                outcome=DispatchOutcome.TIMED_OUT,
                exit_code=output.result.exit_code,
                payload={},
                failure_category="timeout",
            )
        try:
            document: Any = json.loads(output.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return DispatchResult(
                assignment_id=request.assignment_id,
                invocation_id=request.invocation_id,
                client_id=request.client_id,
                role=request.role,
                outcome=DispatchOutcome.FAILED,
                exit_code=output.result.exit_code,
                payload={},
                failure_category="unparsable-result",
            )
        return parse_result(self.descriptor, request, document)

    def _assert_cancellable(self) -> None:
        """Timeout iptal demektir; istemci iptali desteklemiyorsa gorunur olsun."""

        if not self.descriptor.supports("cancellation"):
            raise PolicyViolation("istemci iptal yetenegi beyan etmiyor; timeout ele alinamaz")


def codex_adapter(
    executable: str,
    *,
    cancellation: bool = True,
    permission_manifest: ClientPermissionManifest | None = None,
) -> SubprocessClientAdapter:
    capabilities = {"chat", "code", "tool-use", "structured-result", "sandbox-write"}
    if cancellation:
        capabilities.add("cancellation")
    return SubprocessClientAdapter(
        ClientDescriptor(
            kind=ClientKind.CODEX,
            client_id="codex",
            executable=executable,
            capabilities=frozenset(capabilities),
            permission_manifest=permission_manifest,
        )
    )


def claude_code_adapter(
    executable: str,
    *,
    permission_manifest: ClientPermissionManifest | None = None,
) -> SubprocessClientAdapter:
    return SubprocessClientAdapter(
        ClientDescriptor(
            kind=ClientKind.CLAUDE_CODE,
            client_id="claude-code",
            executable=executable,
            capabilities=frozenset(
                {
                    "chat",
                    "code",
                    "tool-use",
                    "structured-result",
                    "parallel-dispatch",
                    "cancellation",
                    "sandbox-write",
                }
            ),
            permission_manifest=permission_manifest,
        )
    )


def opencode_adapter(
    executable: str,
    *,
    permission_manifest: ClientPermissionManifest | None = None,
) -> SubprocessClientAdapter:
    return SubprocessClientAdapter(
        ClientDescriptor(
            kind=ClientKind.OPENCODE,
            client_id="opencode",
            executable=executable,
            capabilities=frozenset(
                {"chat", "code", "structured-result", "model-selection", "parallel-dispatch"}
            ),
            permission_manifest=permission_manifest,
        )
    )


@dataclass(frozen=True, slots=True)
class ClientRegistry:
    """Kayitli adapter'lar. Bilinmeyen istemci sessizce turetilmez."""

    adapters: tuple[SubprocessClientAdapter, ...]

    def get(self, client_id: str) -> SubprocessClientAdapter:
        for adapter in self.adapters:
            if adapter.descriptor.client_id == client_id:
                return adapter
        raise PolicyViolation(f"kayitli olmayan istemci: {client_id}")

    def with_capability(self, capability: str) -> tuple[SubprocessClientAdapter, ...]:
        return tuple(
            adapter for adapter in self.adapters if adapter.descriptor.supports(capability)
        )

    def as_dict(self) -> dict[str, Any]:
        return {"adapters": [item.descriptor.as_dict() for item in self.adapters]}
