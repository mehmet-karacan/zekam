"""Process liveness and canonical readiness endpoints for shipped containers."""

from __future__ import annotations

from typing import Any

from zekam import __version__
from zekam.application.composition import build_context, build_doctor
from zekam.domain.app_server_protocol import schema_bundle_digest


def create_health_app() -> Any:
    """Create a minimal API whose readiness is never inferred from liveness."""

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Zekam Runtime", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "schema": "zekam-health/v1",
                "status": "alive",
                "version": __version__,
                "protocol_schema_digest": schema_bundle_digest(),
                "grants_authority": False,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            context = build_context()
            report = build_doctor(context).run(categories=("core", "postgres", "runtime"))
            ready = report.overall.value == "healthy"
            checks = [
                {"check_id": item.check_id, "status": item.status.value} for item in report.results
            ]
            document = {
                "schema": "zekam-readiness/v1",
                "status": "ready" if ready else "not-ready",
                "checks": checks,
                "grants_authority": False,
            }
        except Exception as exc:  # fail closed; only sanitized category is exposed
            ready = False
            document = {
                "schema": "zekam-readiness/v1",
                "status": "not-ready",
                "error_category": type(exc).__name__,
                "grants_authority": False,
            }
        return JSONResponse(
            document,
            status_code=200 if ready else 503,
            headers={"Cache-Control": "no-store"},
        )

    return app
