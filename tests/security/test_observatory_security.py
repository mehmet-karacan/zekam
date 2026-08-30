from __future__ import annotations

import asyncio
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from zekam.application.observatory import (
    CompositeRuntimeProjectionReader,
    LocalSessionFileProjectionReader,
    ObservatoryService,
)
from zekam.domain.process_observation import ProcessObservationSnapshot
from zekam.interfaces.api.observatory import create_app


class NoProcesses:
    def read(self) -> ProcessObservationSnapshot:
        import datetime as dt

        return ProcessObservationSnapshot(
            dt.datetime.now(dt.UTC),
            (),
            False,
            "process-observation-disabled",
        )


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            child_key for child in value.values() for child_key in _keys(child)
        }
    if isinstance(value, list):
        return {child_key for child in value for child_key in _keys(child)}
    return set()


def _request(
    app: Any,
    method: str,
    path: str,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    async def invoke() -> tuple[int, dict[str, str], bytes]:
        sent: list[dict[str, Any]] = []
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "headers": ((b"host", b"testserver"),),
                "client": ("127.0.0.1", 50000),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )
        start = next(message for message in sent if message["type"] == "http.response.start")
        chunks = [
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        ]
        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in start["headers"]
        }
        return int(start["status"]), headers, b"".join(chunks)

    return asyncio.run(invoke())


def test_snapshot_excludes_raw_content_paths_commands_and_secrets(tmp_path: Path) -> None:
    root = tmp_path / "core"
    sessions = tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    secret = "token" + "=" + "zekam-must-never-leak"
    transcript = "RAW TERMINAL OUTPUT private command line"
    (root / "README.md").write_text(
        f"# Zekam\n\n{secret}\n{transcript}\n",
        encoding="utf-8",
    )
    (sessions / "rollout-01a02b31-a697-7553-8a72-c5ba348997a2.jsonl").write_text(
        json.dumps({"prompt": secret, "response": transcript}),
        encoding="utf-8",
    )
    reader = CompositeRuntimeProjectionReader(
        (LocalSessionFileProjectionReader("codex", sessions),),
        process_reader=NoProcesses(),
    )

    document = ObservatoryService(root, client_reader=reader).snapshot().as_dict()
    encoded = json.dumps(document, ensure_ascii=False)
    keys = _keys(document)

    assert secret not in encoded
    assert transcript not in encoded
    assert str(tmp_path) not in encoded
    assert document["safety"] == {
        "prompt_content": False,
        "model_response_content": False,
        "secret_values": False,
        "authority": False,
    }
    forbidden_keys = {
        "argv",
        "cmdline",
        "command_line",
        "environment",
        "prompt",
        "response",
        "transcript",
        "terminal_output",
        "tool_input",
        "tool_output",
        "outbox_payload",
        "owner_credential",
    }
    assert forbidden_keys.isdisjoint(keys)


def test_http_surface_is_read_only_and_keeps_security_headers(context: Any) -> None:
    app = create_app(context)
    observatory_routes = [
        route
        for route in app.routes
        if str(getattr(route, "path", "")).startswith("/api/observatory")
    ]

    assert observatory_routes
    assert all(
        not ({"POST", "PUT", "PATCH", "DELETE"} & set(getattr(route, "methods", ()) or ()))
        for route in observatory_routes
    )

    status, headers, body = _request(app, "GET", "/api/observatory/snapshot")
    rejected_status, _, _ = _request(app, "POST", "/api/observatory/snapshot", b"{}")

    assert status == 200
    assert rejected_status == 405
    assert headers["cache-control"] == "no-store"
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "connect-src 'self'" in headers["content-security-policy"]
    assert "http:" not in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert json.loads(body)["runtime"]["detail"] == "realm-id-required"


def test_browser_projection_has_no_unsafe_content_sink_or_remote_dependency() -> None:
    static = files("zekam.interfaces.api").joinpath("static")
    index = static.joinpath("index.html").read_text(encoding="utf-8")
    script = static.joinpath("app.js").read_text(encoding="utf-8")

    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "localStorage",
        "sessionStorage",
    ):
        assert forbidden not in script
    assert "textContent" in script
    assert "https://" not in index
    assert "http://" not in index
    assert "raw command line" not in script.casefold()
    assert "prompt_content" not in script
    assert "model_response" not in script
