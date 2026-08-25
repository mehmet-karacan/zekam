from __future__ import annotations

import asyncio
import json

import pytest

from zekam.interfaces.api import health

pytestmark = pytest.mark.unit


def test_health_is_process_only_and_readiness_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    monkeypatch.setattr(health, "build_context", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    app = health.create_health_app()
    endpoints = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}

    live = asyncio.run(endpoints["/healthz"]())
    ready = asyncio.run(endpoints["/readyz"]())
    live_document = json.loads(live.body)
    ready_document = json.loads(ready.body)

    assert live.status_code == 200
    assert live_document["status"] == "alive"
    assert live_document["grants_authority"] is False
    assert ready.status_code == 503
    assert ready_document == {
        "schema": "zekam-readiness/v1",
        "status": "not-ready",
        "error_category": "RuntimeError",
        "grants_authority": False,
    }
