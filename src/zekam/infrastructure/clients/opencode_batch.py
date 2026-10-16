"""Native OpenCode V2 batch protocol for CAS-backed test agent assignments.

The model returns a patch/analysis, never edits the source tree itself. Zekam
retains assignment-first dispatch and applies validated test patches separately.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.object_store import ObjectStore
from zekam.application.unit_test_secret_scan import find_secret_value
from zekam.domain.canonical import canonical_bytes, canonical_json, digest_of_bytes, parse_digest
from zekam.domain.clients import (
    CanonicalDispatchPermit,
    ClientDescriptor,
    ClientKind,
    DispatchOutcome,
    DispatchRequest,
    DispatchResult,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.sandbox import ProcessSpec
from zekam.infrastructure.clients.adapters import SubprocessClientAdapter
from zekam.infrastructure.clients.opencode_executable import resolve_opencode_executable
from zekam.infrastructure.process import runner

MAX_CONTEXT_BYTES = 200_000
MAX_INSTRUCTION_BYTES = 16_384
MAX_RESULT_BYTES = 1_000_000
SUPPORTED_VERSION = "2.0.29"
_AGENT = "zekam-test-batch"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValidationFailed("OpenCode batch duplicate JSON key")
        document[key] = value
    return document


def _json(raw: str) -> dict[str, Any]:
    def reject_constant(_: str) -> None:
        raise ValidationFailed("OpenCode batch nonfinite JSON")

    try:
        document = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ValidationFailed("OpenCode batch strict JSON olmali") from exc
    if not isinstance(document, dict):
        raise ValidationFailed("OpenCode batch JSON object olmali")
    return document


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise ValidationFailed("OpenCode batch event identity gecersiz")
    return value


def parse_batch_events(raw: bytes) -> dict[str, Any]:
    """Accept one completed text-only native step; discard reasoning entirely."""

    if len(raw) > MAX_RESULT_BYTES:
        raise ValidationFailed("OpenCode batch output siniri asildi")
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise ValidationFailed("OpenCode batch UTF-8 olmali") from exc
    session: str | None = None
    message: str | None = None
    started = False
    finished = False
    text: str | None = None
    text_id: str | None = None
    for line in lines:
        if not line.strip():
            continue
        event = _json(line)
        current_session = _identifier(event.get("sessionID"))
        if session is not None and session != current_session:
            raise ValidationFailed("OpenCode batch root session drift")
        session = current_session
        kind = event.get("type")
        if kind not in {"step_start", "text", "reasoning", "step_finish"}:
            # Includes error, tool_use, future unknown protocol events. Never
            # publish the raw provider error or tool content into a receipt.
            raise ValidationFailed("OpenCode batch error/tool/unknown event")
        if finished:
            raise ValidationFailed("OpenCode batch terminal sonrasi event")
        part = event.get("part")
        if not isinstance(part, dict) or part.get("sessionID") != session:
            raise ValidationFailed("OpenCode batch part session drift")
        current_message = _identifier(part.get("messageID"))
        if message is not None and message != current_message:
            raise ValidationFailed("OpenCode batch assistant message drift")
        message = current_message
        if kind == "step_start":
            if started:
                raise ValidationFailed("OpenCode batch multiple steps")
            started = True
        elif not started:
            raise ValidationFailed("OpenCode batch step start eksik")
        elif kind == "text":
            current_text_id = _identifier(part.get("id"))
            if text_id is not None and current_text_id != text_id:
                raise ValidationFailed("OpenCode batch multiple text parts")
            if not isinstance(part.get("text"), str):
                raise ValidationFailed("OpenCode batch text eksik")
            # Native CLI can publish updated/full content for the same part;
            # this is replacement, never concatenation of repeated snapshots.
            text_id, text = current_text_id, part["text"]
        elif kind == "step_finish":
            if part.get("reason") != "stop" or text is None:
                raise ValidationFailed("OpenCode batch completed stop eksik")
            finished = True
        # reasoning is deliberately neither collected nor persisted.
    if not finished or session is None or message is None or text is None:
        raise ValidationFailed("OpenCode batch incomplete event stream")
    document = _json(text)
    if set(document) != {"result", "body"} or not isinstance(document["body"], dict):
        raise ValidationFailed("OpenCode batch result/body sozlesmesi gecersiz")
    if not isinstance(document["result"], dict):
        raise ValidationFailed("OpenCode batch result object olmali")
    if find_secret_value(document) is not None:
        raise PolicyViolation("OpenCode batch result secret degeri tasiyor")
    # Model-supplied agent_id is not execution identity. Use actual native event
    # session/message IDs; each dispatch creates a fresh private-server run.
    document["result"]["agent_id"] = f"opencode:{session}:{message}"
    return document


def batch_config() -> str:
    deny = [{"action": "*", "resource": "*", "effect": "deny"}]
    return canonical_json(
        {
            "snapshots": False,
            "permissions": deny,
            "agents": {
                _AGENT: {
                    "mode": "primary",
                    "steps": 1,
                    "system": "Return only the requested JSON. Use supplied context only. No tools.",
                    "permissions": deny,
                }
            },
            "experimental": {
                "policies": [
                    {"action": "tool.use", "resource": "*", "effect": "deny"},
                    {"action": "integration.use", "resource": "*", "effect": "deny"},
                ]
            },
        }
    )


@dataclass(frozen=True, slots=True)
class OpenCodeBatchAdapter(SubprocessClientAdapter):
    """Native V2 run/events, distinct from the generic wrapper flag protocol.

    Isolated config/home hide user agent/plugin overrides; project discovery is
    disabled. Built-in provider credentials remain owned by OpenCode. Custom
    global provider definitions are not imported into this restricted profile.
    """

    objects: ObjectStore | None = None
    network_authorized: bool = False

    def _object(self, key: str, limit: int) -> bytes:
        parse_digest(key)
        if self.objects is None:
            raise PolicyViolation("OpenCode batch CAS eksik")
        info = self.objects.stat(key)
        if info.size_bytes > limit:
            raise ValidationFailed("OpenCode batch CAS boyut siniri asildi")
        raw = self.objects.get(key)
        if len(raw) > limit or len(raw) != info.size_bytes or digest_of_bytes(raw) != key:
            raise ValidationFailed("OpenCode batch CAS icerik bagi bozuldu")
        return raw

    def dispatch(
        self, request: DispatchRequest, *, cwd: Path, permit: CanonicalDispatchPermit
    ) -> DispatchResult:
        permit.assert_valid(request)
        deadline = time.monotonic() + request.timeout_seconds
        if request.client_id != self.descriptor.client_id:
            raise PolicyViolation("OpenCode batch client identity drift")
        if self.model_id is None or "/" not in self.model_id or self.model_id.startswith("-"):
            raise PolicyViolation("OpenCode batch explicit provider/model ister")
        if not self.network_authorized:
            raise PolicyViolation(
                "OpenCode V2 batch builtin provider icin explicit remote/network onayi ister"
            )
        instruction = self._object(request.instruction_digest, MAX_INSTRUCTION_BYTES)
        context = self._object(request.context_manifest_digest, MAX_CONTEXT_BYTES)
        try:
            instruction_text = instruction.decode("utf-8", errors="strict")
            context_doc = _json(context.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as exc:
            raise ValidationFailed("OpenCode batch CAS UTF-8 olmali") from exc
        if find_secret_value({"instruction": instruction_text, "context": context_doc}) is not None:
            raise PolicyViolation("OpenCode batch prompt secret degeri tasiyor")
        prompt = canonical_bytes(
            {
                "instruction": instruction_text,
                "context": context_doc,
                "contract": "zekam-test-batch/v1",
            }
        )
        executable = str(resolve_opencode_executable(self.descriptor.executable))

        def result(
            category: str, exit_code: int | None, *, timeout: bool = False
        ) -> DispatchResult:
            return DispatchResult(
                assignment_id=request.assignment_id,
                invocation_id=request.invocation_id,
                client_id=request.client_id,
                role=request.role,
                outcome=DispatchOutcome.TIMED_OUT if timeout else DispatchOutcome.FAILED,
                exit_code=exit_code,
                payload={},
                failure_category=category,
            )

        if Path(tempfile.gettempdir()).resolve().is_relative_to(cwd.resolve()):
            raise PolicyViolation("OpenCode batch temp source kokunde olamaz")
        # These are temporary runtime controls, never a project clone or source
        # tree. No prompt/config file is written in the source checkout.
        with tempfile.TemporaryDirectory(prefix="zekam-opencode-batch-") as directory:
            temporary = Path(directory).resolve()
            if temporary.is_relative_to(cwd.resolve()):
                raise PolicyViolation("OpenCode batch temp source kokunde olamaz")
            config_dir = temporary / "config"
            private_home = temporary / "home"
            config_dir.mkdir()
            private_home.mkdir()
            version = runner.run(
                ProcessSpec(
                    argv=(executable, "--version"),
                    timeout_seconds=min(5, request.timeout_seconds),
                    max_output_bytes=4096,
                ),
                cwd=temporary,
            )
            if version.result.timed_out:
                return result("timeout", version.result.exit_code, timeout=True)
            if not version.result.succeeded or version.stdout.strip() != SUPPORTED_VERSION.encode():
                return result("unsupported-opencode-version", version.result.exit_code)
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                return result("timeout", None, timeout=True)
            env = (
                ("OPENCODE_CONFIG_DIR", str(config_dir)),
                ("OPENCODE_TEST_HOME", str(private_home)),
                ("OPENCODE_CONFIG_PROJECT_DISABLE", "1"),
                ("OPENCODE_CONFIG_CONTENT", batch_config()),
                ("OPENCODE_DISABLE_MODELS_FETCH", "1"),
            )
            output = runner.run(
                ProcessSpec(
                    argv=(
                        executable,
                        "run",
                        "--standalone",
                        "--format",
                        "json",
                        "--agent",
                        _AGENT,
                        "--model",
                        self.model_id,
                    ),
                    env=env,
                    stdin_digest=digest_of_bytes(prompt),
                    timeout_seconds=remaining,
                    max_output_bytes=MAX_RESULT_BYTES,
                ),
                cwd=cwd,
                stdin=prompt,
            )
        if output.result.timed_out:
            return result("timeout", output.result.exit_code, timeout=True)
        if time.monotonic() > deadline:
            return result("timeout", output.result.exit_code, timeout=True)
        if output.result.truncated:
            return result("output-limit", output.result.exit_code)
        if output.result.exit_code != 0:
            return result("process-exit", output.result.exit_code)
        try:
            document = parse_batch_events(output.stdout)
        except (ValidationFailed, PolicyViolation):
            return result("invalid-native-batch-result", output.result.exit_code)
        assert self.objects is not None
        artifact = self.objects.put(canonical_bytes(document), media_type="application/json")
        return DispatchResult(
            assignment_id=request.assignment_id,
            invocation_id=request.invocation_id,
            client_id=request.client_id,
            role=request.role,
            outcome=DispatchOutcome.SUCCESS,
            exit_code=output.result.exit_code,
            payload={"artifact_digest": artifact.digest},
        )


def opencode_batch_adapter(
    executable: str, *, objects: ObjectStore, model_id: str | None, network_authorized: bool = False
) -> OpenCodeBatchAdapter:
    return OpenCodeBatchAdapter(
        descriptor=ClientDescriptor(
            kind=ClientKind.OPENCODE,
            client_id="opencode",
            executable=executable,
            capabilities=frozenset(
                {"chat", "code", "structured-result", "model-selection", "cancellation"}
            ),
            version=SUPPORTED_VERSION,
        ),
        objects=objects,
        model_id=model_id,
        network_authorized=network_authorized,
    )
