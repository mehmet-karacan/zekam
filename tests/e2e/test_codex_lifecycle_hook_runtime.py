"""Run the reviewed Codex binary against hooks and a loopback Responses stub."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_REVIEWED_VERSION,
    CODEX_REVIEWED_WINDOWS_SHA256,
    parse_codex_version_output,
)

pytestmark = pytest.mark.e2e
MODEL = "zekam-loopback-model"
PROMPT_MARKER = "E2E-CONTENT-MUST-NOT-PERSIST"


def _codex_executable() -> str:
    if os.name != "nt":
        pytest.skip("Reviewed Codex lifecycle contract Windows x86_64 icindir")
    configured = os.environ.get("CODEX_EXECUTABLE")
    executable = configured or shutil.which("codex")
    if executable is None:
        pytest.skip("Codex runtime bulunamadi")
    return executable


def _response_document(*, completed: bool) -> dict[str, Any]:
    status = "completed" if completed else "in_progress"
    content = {
        "type": "output_text",
        "text": "READY",
        "annotations": [],
        "logprobs": [],
    }
    message = {
        "id": "msg_loopback_1",
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [content] if completed else [],
    }
    return {
        "id": "resp_loopback_1",
        "object": "response",
        "created_at": 0,
        "status": status,
        "background": False,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": MODEL,
        "output": [message] if completed else [],
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "service_tier": "default",
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        },
        "metadata": {},
    }


def _responses_sse() -> bytes:
    pending = _response_document(completed=False)
    completed = _response_document(completed=True)
    content = completed["output"][0]["content"][0]
    message = completed["output"][0]
    partial_message = {**message, "status": "in_progress", "content": []}
    events = (
        {"type": "response.created", "response": pending, "sequence_number": 0},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": partial_message,
            "sequence_number": 1,
        },
        {
            "type": "response.content_part.added",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {**content, "text": ""},
            "sequence_number": 2,
        },
        {
            "type": "response.output_text.delta",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": "READY",
            "logprobs": [],
            "sequence_number": 3,
        },
        {
            "type": "response.output_text.done",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "text": "READY",
            "logprobs": [],
            "sequence_number": 4,
        },
        {
            "type": "response.content_part.done",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "part": content,
            "sequence_number": 5,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": message,
            "sequence_number": 6,
        },
        {"type": "response.completed", "response": completed, "sequence_number": 7},
    )
    chunks = [
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
        for event in events
    ]
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode("utf-8")


class _ResponsesHandler(BaseHTTPRequestHandler):
    server_version = "ZekamLoopbackResponses/1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)  # Drain but never persist prompt/request content.
        self.server.request_paths.append(self.path)  # type: ignore[attr-defined]
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        payload = _responses_sse()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class _DenyProxyHandler(BaseHTTPRequestHandler):
    def _deny(self) -> None:
        self.server.request_paths.append(self.path)  # type: ignore[attr-defined]
        self.send_error(502, "proxy-routed request denied by E2E")

    def do_CONNECT(self) -> None:
        self._deny()

    def do_GET(self) -> None:
        self._deny()

    def do_POST(self) -> None:
        self._deny()

    def log_message(self, format: str, *args: object) -> None:
        return


@contextlib.contextmanager
def _server(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.request_paths = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _hook_arguments(zekam_home: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "zekam.interfaces.cli.client",
        "hook",
        "--client",
        "codex",
        "--client-version",
        CODEX_REVIEWED_VERSION,
        "--home",
        str(zekam_home),
    ]


def _hook_command(zekam_home: Path) -> tuple[str, str]:
    arguments = _hook_arguments(zekam_home)
    return shlex.join(arguments), subprocess.list2cmdline(arguments)


def _write_codex_configuration(
    codex_home: Path,
    *,
    zekam_home: Path,
    responses_port: int,
) -> None:
    codex_home.mkdir(parents=True)
    codex_home.joinpath("config.toml").write_text(
        f'''model = "{MODEL}"
model_provider = "zekam-loopback"
approval_policy = "never"
sandbox_mode = "read-only"

[features]
hooks = true

[model_providers.zekam-loopback]
name = "Zekam E2E loopback"
base_url = "http://127.0.0.1:{responses_port}/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0

[otel]
exporter = "none"
''',
        encoding="utf-8",
        newline="\n",
    )
    command, command_windows = _hook_command(zekam_home)

    def group(timeout: int) -> list[dict[str, object]]:
        return [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "commandWindows": command_windows,
                        "timeout": timeout,
                    }
                ]
            }
        ]

    codex_home.joinpath("hooks.json").write_text(
        json.dumps(
            {
                "description": "Zekam exact Codex lifecycle E2E hooks",
                "hooks": {
                    "SessionStart": group(10),
                    "PreCompact": group(10),
                    "PostCompact": group(10),
                    "Stop": group(10),
                    "SessionEnd": group(3),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _environment(
    *, codex_home: Path, zekam_home: Path, deny_proxy_port: int
) -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
    ):
        environment.pop(key, None)
    source_root = Path(__file__).resolve().parents[2] / "src"
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root)
        if not existing_pythonpath
        else str(source_root) + os.pathsep + existing_pythonpath
    )
    environment["CODEX_HOME"] = str(codex_home)
    environment["ZEKAM_HOME"] = str(zekam_home)
    deny_proxy = f"http://127.0.0.1:{deny_proxy_port}"
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment[key] = deny_proxy
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    return environment


def test_real_codex_command_hooks_spool_content_free_loopback_lifecycle(
    tmp_path: Path,
) -> None:
    codex = _codex_executable()
    version_result = subprocess.run(
        [codex, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert version_result.returncode == 0, version_result.stderr
    assert parse_codex_version_output(version_result.stdout) == CODEX_REVIEWED_VERSION
    assert (
        hashlib.sha256(Path(codex).read_bytes()).hexdigest()
        == CODEX_REVIEWED_WINDOWS_SHA256
    )

    codex_home = tmp_path / "codex-home"
    zekam_home = tmp_path / "zekam-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with _server(_ResponsesHandler) as responses, _server(_DenyProxyHandler) as deny_proxy:
        _write_codex_configuration(
            codex_home,
            zekam_home=zekam_home,
            responses_port=responses.server_port,
        )
        environment = _environment(
            codex_home=codex_home,
            zekam_home=zekam_home,
            deny_proxy_port=deny_proxy.server_port,
        )
        result = subprocess.run(
            [
                codex,
                "exec",
                "--ephemeral",
                "--json",
                "--dangerously-bypass-hook-trust",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-a",
                "never",
                "-m",
                MODEL,
                f"Return READY. Marker: {PROMPT_MARKER}",
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        # The normal run exercises events the real binary emits naturally.
        # Compaction is not forced through an unsupported Codex flag: instead,
        # the same installed hook entrypoint receives the two official wire
        # envelopes to prove Pre/PostCompact parser and spool parity.
        natural_entries = ClientLifecycleSpool(
            zekam_home, client_id="codex"
        ).pending(limit=256)
        stop = next(item for item in natural_entries if item.external_event_type == "Stop")
        turn_id = stop.observation["turn_id"]
        assert isinstance(turn_id, str)
        for event_name in ("PreCompact", "PostCompact"):
            hook = subprocess.run(
                _hook_arguments(zekam_home),
                input=json.dumps(
                    {
                        "session_id": stop.session_id,
                        "hook_event_name": event_name,
                        "turn_id": turn_id,
                        "trigger": "manual",
                    }
                ),
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert hook.returncode == 0, hook.stdout + hook.stderr
            assert json.loads(hook.stdout) == {}

    assert result.returncode == 0, result.stdout + result.stderr
    stream = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(item.get("type") == "turn.completed" for item in stream)
    # These are route/proxy observations, not a kernel-level egress-deny proof.
    assert responses.request_paths == ["/v1/responses"]  # type: ignore[attr-defined]
    assert deny_proxy.request_paths == []  # type: ignore[attr-defined]

    entries = ClientLifecycleSpool(zekam_home, client_id="codex").pending(limit=256)
    raw_to_canonical = {
        item.external_event_type: item.internal_event_type for item in entries
    }
    assert raw_to_canonical["SessionStart"] == "session_start"
    assert raw_to_canonical["PreCompact"] == "pre_compaction"
    assert raw_to_canonical["PostCompact"] == "post_compaction"
    assert raw_to_canonical["Stop"] == "pre_close"
    assert raw_to_canonical["SessionEnd"] == "post_close"
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(
            (zekam_home / "global" / "runtime" / "client-lifecycle" / "codex" / "events").glob(
                "*.json"
            )
        )
    )
    assert PROMPT_MARKER not in persisted
    assert "READY" not in persisted
    assert str(workspace) not in persisted
    assert "transcript_path" not in persisted
