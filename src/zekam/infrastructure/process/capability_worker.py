"""Capability lane'leri icin bounded JSON IPC ve sert process-tree siniri.

Child process stdin uzerinden tek satirlik ``execute`` envelope'i alir. Deadline
asilirsa ayni kanaldan ``cancel`` envelope'i gonderilir; varsayilan on saniyelik
grace sonunda halen calisan butun process tree sert sonlandirilir. Deadline'dan
sonra gelen sonuc hicbir kosulda cagirana yayimlanmaz.

Bu katman stderr veya ham process ciktisini loglamaz. Yalniz dogrulanmis, bounded
JSON result envelope'ini process belleginde cagirana dondurur.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from zekam.domain.canonical import parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_invocation import GatewayTransportProvenance
from zekam.domain.security import SecretValue
from zekam.infrastructure.process.bounded_stream import (
    BoundedReader,
    finish_pipes,
    hard_kill_tree,
    start_process_tree,
    wait_for,
    worker_env,
)

CAPABILITY_WORKER_SCHEMA: Final = "zekam-capability-worker/v1"
DEFAULT_MAX_IPC_BYTES: Final = 1_048_576
DEFAULT_CANCELLATION_GRACE_SECONDS: Final = 10.0
DEFAULT_PROVIDER_RESPONSE_BYTES: Final = 4 * 1024 * 1024


class CapabilityWorkerStatus(StrEnum):
    """Sanitized parent-side terminal outcomes."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    PROTOCOL_ERROR = "protocol-error"
    OUTPUT_LIMIT = "output-limit"


@dataclass(frozen=True, slots=True)
class CapabilityWorkerRequest:
    """Typed execution request. Payload is never included in repr or logs."""

    request_id: str
    payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.request_id.strip() or len(self.request_id) > 128:
            raise ValidationFailed("Capability worker request kimligi gecersiz")
        if not isinstance(self.payload, Mapping):
            raise ValidationFailed("Capability worker payload object olmali")

    def as_message(self) -> dict[str, Any]:
        return {
            "schema": CAPABILITY_WORKER_SCHEMA,
            "type": "execute",
            "request_id": self.request_id,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class CapabilityWorkerSpec:
    """Shell-free process boundary and hard wall deadline."""

    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    max_ipc_bytes: int = DEFAULT_MAX_IPC_BYTES

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(part, str) or not part for part in self.argv):
            raise ValidationFailed("Capability worker argv gecersiz")
        if not self.cwd.is_dir():
            raise PolicyViolation("Capability worker calisma dizini bulunamadi")
        if not 0 < self.timeout_seconds <= 300:
            raise PolicyViolation("Capability worker timeout 0..300 saniye olmali")
        if not 1_024 <= self.max_ipc_bytes <= 16_777_216:
            raise PolicyViolation("Capability worker IPC byte siniri gecersiz")


@dataclass(frozen=True, slots=True)
class CapabilityWorkerResult:
    """Validated response or a sanitized infrastructure outcome."""

    request_id: str
    status: CapabilityWorkerStatus
    payload: Mapping[str, Any] | None = field(default=None, repr=False)
    error_code: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    cancel_sent: bool = False
    hard_killed: bool = False
    late_result_suppressed: bool = False


class CapabilityProcessWorker:
    """Run one typed capability request in a dedicated process tree."""

    def __init__(self, *, cancellation_grace_seconds: float = DEFAULT_CANCELLATION_GRACE_SECONDS):
        if not 0 < cancellation_grace_seconds <= DEFAULT_CANCELLATION_GRACE_SECONDS:
            raise PolicyViolation("Capability worker cancellation grace gecersiz")
        self._cancellation_grace_seconds = cancellation_grace_seconds

    def run(
        self,
        spec: CapabilityWorkerSpec,
        request: CapabilityWorkerRequest,
    ) -> CapabilityWorkerResult:
        started = time.monotonic()
        request_bytes = _encode_message(request.as_message(), spec.max_ipc_bytes)
        tree = start_process_tree(spec.argv, spec.cwd, env=worker_env())
        process = tree.process
        assert process.stdin is not None
        assert process.stdout is not None
        reader = BoundedReader(process.stdout, spec.max_ipc_bytes)
        reader_thread = threading.Thread(target=reader.read, daemon=True)
        reader_thread.start()

        try:
            process.stdin.write(request_bytes)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            hard_kill_tree(tree)
            finish_pipes(tree, (reader_thread,))
            return self._result(
                request.request_id,
                CapabilityWorkerStatus.FAILED,
                started,
                process,
                error_code="worker-start-failed",
                hard_killed=True,
            )

        deadline = started + spec.timeout_seconds
        outcome = wait_for(process, deadline, reader.overflow)
        if outcome == "overflow":
            hard_kill_tree(tree)
            finish_pipes(tree, (reader_thread,))
            return self._result(
                request.request_id,
                CapabilityWorkerStatus.OUTPUT_LIMIT,
                started,
                process,
                error_code="ipc-output-limit",
                hard_killed=True,
            )

        if outcome == "deadline":
            cancel_sent = _send_cancel(process, request.request_id, spec.max_ipc_bytes)
            grace_deadline = time.monotonic() + self._cancellation_grace_seconds
            grace_outcome = wait_for(process, grace_deadline, reader.overflow)
            hard_killed = grace_outcome != "exited" and process.poll() is None
            if hard_killed:
                hard_kill_tree(tree)
            finish_pipes(tree, (reader_thread,))
            return self._result(
                request.request_id,
                CapabilityWorkerStatus.TIMEOUT,
                started,
                process,
                error_code="deadline-exceeded",
                cancel_sent=cancel_sent,
                hard_killed=hard_killed,
                late_result_suppressed=bool(reader.buffer),
            )

        finish_pipes(tree, (reader_thread,))
        if reader.overflow.is_set():
            return self._result(
                request.request_id,
                CapabilityWorkerStatus.OUTPUT_LIMIT,
                started,
                process,
                error_code="ipc-output-limit",
            )
        return self._decode_result(request.request_id, bytes(reader.buffer), started, process)

    @staticmethod
    def _result(
        request_id: str,
        status: CapabilityWorkerStatus,
        started: float,
        process: subprocess.Popen[bytes],
        **kwargs: Any,
    ) -> CapabilityWorkerResult:
        return CapabilityWorkerResult(
            request_id=request_id,
            status=status,
            exit_code=process.poll(),
            duration_ms=int((time.monotonic() - started) * 1000),
            **kwargs,
        )

    def _decode_result(
        self,
        request_id: str,
        raw: bytes,
        started: float,
        process: subprocess.Popen[bytes],
    ) -> CapabilityWorkerResult:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._result(
                request_id,
                CapabilityWorkerStatus.PROTOCOL_ERROR,
                started,
                process,
                error_code="invalid-json",
            )
        if not isinstance(document, dict) or set(document) != {
            "schema",
            "type",
            "request_id",
            "status",
            "payload",
            "error_code",
        }:
            return self._result(
                request_id,
                CapabilityWorkerStatus.PROTOCOL_ERROR,
                started,
                process,
                error_code="invalid-envelope",
            )
        if (
            document["schema"] != CAPABILITY_WORKER_SCHEMA
            or document["type"] != "result"
            or document["request_id"] != request_id
            or document["status"] not in {"completed", "failed"}
            or (document["payload"] is not None and not isinstance(document["payload"], dict))
            or (document["error_code"] is not None and not isinstance(document["error_code"], str))
        ):
            return self._result(
                request_id,
                CapabilityWorkerStatus.PROTOCOL_ERROR,
                started,
                process,
                error_code="invalid-envelope",
            )
        status = (
            CapabilityWorkerStatus.COMPLETED
            if document["status"] == "completed"
            else CapabilityWorkerStatus.FAILED
        )
        return self._result(
            request_id,
            status,
            started,
            process,
            payload=document["payload"],
            error_code=document["error_code"],
        )


@dataclass(frozen=True, slots=True)
class ProcessIsolatedJsonProviderTransport:
    """JsonProviderTransport-compatible, per-call isolated HTTP adapter.

    Credential yalniz SecretValue acikken bounded stdin IPC ile child'a verilir;
    argv, environment, repr, exception veya result alanina tasinmaz.
    """

    timeout_seconds: float = 30.0
    max_response_bytes: int = DEFAULT_PROVIDER_RESPONSE_BYTES
    max_ipc_bytes: int = 8 * 1024 * 1024
    cancellation_grace_seconds: float = DEFAULT_CANCELLATION_GRACE_SECONDS

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 300:
            raise ValidationFailed("Provider process timeout gecersiz")
        if not 1 <= self.max_response_bytes < self.max_ipc_bytes:
            raise ValidationFailed("Provider process response/IPC limiti gecersiz")
        if not 0 < self.cancellation_grace_seconds <= DEFAULT_CANCELLATION_GRACE_SECONDS:
            raise ValidationFailed("Provider process cancellation grace gecersiz")

    def post_json(
        self,
        endpoint: str,
        payload: Mapping[str, Any],
        credential: SecretValue,
        *,
        gateway_provenance: GatewayTransportProvenance,
    ) -> Mapping[str, Any]:
        _validated_provider_endpoint(endpoint)
        parse_digest(gateway_provenance.manifest_digest)
        request = CapabilityWorkerRequest(
            request_id=str(uuid4()),
            payload={
                "operation": "provider-post-json",
                "endpoint": endpoint,
                "payload": dict(payload),
                "credential": credential.reveal(),
                "timeout_seconds": self.timeout_seconds,
                "max_response_bytes": self.max_response_bytes,
                "manifest_digest": gateway_provenance.manifest_digest,
                "gateway_attempt_id": str(gateway_provenance.attempt_id),
                "gateway_claim_id": str(gateway_provenance.claim_id),
            },
        )
        spec = CapabilityWorkerSpec(
            argv=(sys.executable, "-m", __name__, "--provider-child"),
            cwd=Path.cwd(),
            timeout_seconds=self.timeout_seconds,
            max_ipc_bytes=self.max_ipc_bytes,
        )
        outcome = CapabilityProcessWorker(
            cancellation_grace_seconds=self.cancellation_grace_seconds
        ).run(spec, request)
        if outcome.status is not CapabilityWorkerStatus.COMPLETED or outcome.payload is None:
            # Runtime import avoids an infrastructure -> application import cycle.
            from zekam.application.model_health_service import ProbeUnavailable

            raise ProbeUnavailable(_provider_failure_message(outcome))
        response = outcome.payload.get("provider_response")
        if not isinstance(response, dict):
            from zekam.application.model_health_service import ProbeUnavailable

            raise ProbeUnavailable("Provider child response gecersiz")
        return response


def _encode_message(message: Mapping[str, Any], limit: int) -> bytes:
    try:
        encoded = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("Capability worker JSON payload gecersiz") from exc
    if len(encoded) > limit:
        raise PolicyViolation("Capability worker IPC request byte sinirini asti")
    return encoded


def _send_cancel(process: subprocess.Popen[bytes], request_id: str, limit: int) -> bool:
    if process.stdin is None or process.poll() is not None:
        return False
    message = {
        "schema": CAPABILITY_WORKER_SCHEMA,
        "type": "cancel",
        "request_id": request_id,
    }
    try:
        process.stdin.write(_encode_message(message, limit))
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        return False
    return True


def _validated_provider_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationFailed("Provider endpoint userinfo, query veya fragment tasiyamaz")
    if not parsed.hostname or not parsed.path.startswith("/"):
        raise ValidationFailed("Provider endpoint absolute URL olmali")
    if parsed.scheme == "https":
        return value
    if parsed.scheme != "http":
        raise ValidationFailed("Provider endpoint HTTPS olmali")
    try:
        loopback = ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() == "localhost"
    if not loopback:
        raise ValidationFailed("HTTP yalniz loopback provider endpoint icin kabul edilir")
    return value


class _ChildNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None


def _provider_failure_message(outcome: CapabilityWorkerResult) -> str:
    if outcome.status is CapabilityWorkerStatus.TIMEOUT:
        return "Provider process hard deadline asildi"
    if outcome.status is CapabilityWorkerStatus.OUTPUT_LIMIT:
        return "Provider process IPC boyut sinirini asti"
    if outcome.error_code and (
        outcome.error_code.startswith("provider-http-status-")
        or outcome.error_code
        in {
            "provider-transport-unavailable",
            "provider-response-limit",
            "provider-response-invalid-json",
            "provider-response-not-object",
        }
    ):
        return outcome.error_code
    return "Provider process kullanilamiyor"


def _write_child_result(
    request_id: str,
    status: str,
    payload: Mapping[str, Any] | None,
    error_code: str | None,
) -> None:
    message = {
        "schema": CAPABILITY_WORKER_SCHEMA,
        "type": "result",
        "request_id": request_id,
        "status": status,
        "payload": dict(payload) if payload is not None else None,
        "error_code": error_code,
    }
    sys.stdout.buffer.write(_encode_message(message, 16_777_216))
    sys.stdout.buffer.flush()


def _provider_child() -> int:
    raw = sys.stdin.buffer.readline(16_777_217)
    if len(raw) > 16_777_216:
        return 2
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 2
    if not isinstance(message, dict) or set(message) != {
        "schema",
        "type",
        "request_id",
        "payload",
    }:
        return 2
    request_id = message.get("request_id")
    request_payload = message.get("payload")
    if (
        message.get("schema") != CAPABILITY_WORKER_SCHEMA
        or message.get("type") != "execute"
        or not isinstance(request_id, str)
        or not isinstance(request_payload, dict)
        or set(request_payload)
        != {
            "operation",
            "endpoint",
            "payload",
            "credential",
            "timeout_seconds",
            "max_response_bytes",
            "manifest_digest",
            "gateway_attempt_id",
            "gateway_claim_id",
        }
        or request_payload.get("operation") != "provider-post-json"
    ):
        return 2
    endpoint = request_payload.get("endpoint")
    provider_payload = request_payload.get("payload")
    credential = request_payload.get("credential")
    timeout_seconds = request_payload.get("timeout_seconds")
    max_response_bytes = request_payload.get("max_response_bytes")
    manifest_digest = request_payload.get("manifest_digest")
    gateway_attempt_id = request_payload.get("gateway_attempt_id")
    gateway_claim_id = request_payload.get("gateway_claim_id")
    if (
        not isinstance(endpoint, str)
        or not isinstance(provider_payload, dict)
        or not isinstance(credential, str)
        or not credential
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 300
        or not isinstance(max_response_bytes, int)
        or not 1 <= max_response_bytes <= 16_000_000
    ):
        return 2
    try:
        if not isinstance(manifest_digest, str):
            return 2
        if not isinstance(gateway_attempt_id, str) or not isinstance(gateway_claim_id, str):
            return 2
        parse_digest(manifest_digest)
        UUID(gateway_attempt_id)
        UUID(gateway_claim_id)
    except (TypeError, ValueError, ValidationFailed):
        return 2
    try:
        target = _validated_provider_endpoint(endpoint)
        body = json.dumps(
            provider_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        http_request = urllib.request.Request(
            target,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
        )
        opener = urllib.request.build_opener(_ChildNoRedirect())
        with opener.open(http_request, timeout=float(timeout_seconds)) as response:
            response_raw = response.read(max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        _write_child_result(request_id, "failed", None, f"provider-http-status-{exc.code}")
        return 0
    except (OSError, urllib.error.URLError, TimeoutError):
        _write_child_result(request_id, "failed", None, "provider-transport-unavailable")
        return 0
    if len(response_raw) > max_response_bytes:
        _write_child_result(request_id, "failed", None, "provider-response-limit")
        return 0
    try:
        document = json.loads(response_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _write_child_result(request_id, "failed", None, "provider-response-invalid-json")
        return 0
    if not isinstance(document, dict):
        _write_child_result(request_id, "failed", None, "provider-response-not-object")
        return 0
    _write_child_result(request_id, "completed", {"provider_response": document}, None)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--provider-child"]:
        raise SystemExit(2)
    raise SystemExit(_provider_child())
