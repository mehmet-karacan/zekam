"""FastAPI surface for the read-only Zekam Neuro Observatory."""

import asyncio
import ipaddress
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.application.composition import ApplicationContext
from zekam.application.config import PersistenceBackend
from zekam.application.observatory import (
    CompositeRuntimeProjectionReader,
    EmptyRuntimeProjectionReader,
    LocalSessionFileProjectionReader,
    ObservatoryService,
    OpenCodeLifecycleProjectionReader,
    RuntimeProjectionReader,
)
from zekam.infrastructure.process_observer import BoundedProcessObserver

_STATIC_ROOT = Path(__file__).resolve().parent / "static"


def validated_lan_hosts(values: tuple[str, ...]) -> tuple[str, ...]:
    """Accept only exact, non-loopback interface IPs for explicit LAN exposure."""

    validated: list[str] = []
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("LAN trusted host exact IP olmali") from exc
        if address.is_unspecified or address.is_multicast or address.is_loopback:
            raise ValueError("LAN trusted host belirli bir non-loopback IP olmali")
        validated.append(str(address))
    return tuple(validated)


def create_app(
    context: ApplicationContext,
    *,
    realm_id: UUID | None = None,
    refresh_seconds: float = 2.0,
    allowed_hosts: tuple[str, ...] = (),
) -> Any:
    """Create the local, read-only observatory application.

    FastAPI is optional, therefore all web imports stay inside this factory.
    """

    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
        from starlette.middleware.trustedhost import TrustedHostMiddleware
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("UI icin `pip install 'zekam[api]'` gerekli") from exc

    if refresh_seconds < 0.5 or refresh_seconds > 30:
        raise ValueError("refresh_seconds 0.5..30 araliginda olmali")
    trusted_lan_hosts = validated_lan_hosts(allowed_hosts)
    if not _STATIC_ROOT.is_dir():
        raise RuntimeError("Zekam UI statik dosyalari bulunamadi")

    reader = _runtime_reader(context, realm_id)
    service = ObservatoryService(
        core_path=context.core_path,
        runtime_reader=reader,
        client_reader=CompositeRuntimeProjectionReader(
            (
                OpenCodeLifecycleProjectionReader(
                    context.home,
                    metadata_path=Path.home() / ".local" / "share" / "opencode" / "opencode.db",
                ),
                LocalSessionFileProjectionReader(
                    "codex",
                    Path.home() / ".codex" / "sessions",
                    index_path=Path.home() / ".codex" / "session_index.jsonl",
                ),
                LocalSessionFileProjectionReader(
                    "claude",
                    Path.home() / ".claude" / "projects",
                ),
            ),
            process_reader=BoundedProcessObserver(),
        ),
    )
    app = FastAPI(
        title="Zekam Canlı Yürütme Gözleme Merkezi",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.observatory_service = service
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "127.0.0.1",
            "localhost",
            "::1",
            "[::1]",
            "testserver",
            *trusted_lan_hosts,
        ],
    )
    from zekam.interfaces.api.app_server import install_app_server_routes

    install_app_server_routes(
        app,
        store_factory=_app_server_store_factory(context, realm_id),
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response

    app.mount("/assets", StaticFiles(directory=_STATIC_ROOT), name="observatory-assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_STATIC_ROOT / "index.html", media_type="text/html")

    @app.get("/api/observatory/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "schema": "zekam-observatory-health/v1",
                "status": "ready",
                "read_only": True,
                "grants_authority": False,
                "realm_scoped": realm_id is not None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/observatory/snapshot")
    async def snapshot() -> JSONResponse:
        projection = await asyncio.to_thread(service.snapshot)
        return JSONResponse(
            projection.as_dict(),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/observatory/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream() -> Any:
            last_digest = ""
            heartbeat = 0
            while not await request.is_disconnected():
                projection = await asyncio.to_thread(service.snapshot)
                if projection.projection_digest != last_digest:
                    payload = json.dumps(
                        projection.as_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"event: snapshot\ndata: {payload}\n\n"
                    last_digest = projection.projection_digest
                    heartbeat = 0
                else:
                    heartbeat += 1
                    if heartbeat >= 8:
                        yield ": heartbeat\n\n"
                        heartbeat = 0
                await asyncio.sleep(refresh_seconds)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _runtime_reader(
    context: ApplicationContext,
    realm_id: UUID | None,
) -> RuntimeProjectionReader:
    if realm_id is None:
        return EmptyRuntimeProjectionReader("realm-id-required")
    if context.settings.database.backend is not PersistenceBackend.POSTGRESQL:
        return EmptyRuntimeProjectionReader("postgresql-required-for-live-runtime")
    from zekam.infrastructure.postgres.observatory_projection import (
        PostgresObservatoryProjectionReader,
    )

    return PostgresObservatoryProjectionReader(context.settings.database, realm_id)


def _app_server_store_factory(context: ApplicationContext, realm_id: UUID | None) -> Any:
    if realm_id is None or context.settings.database.backend is not PersistenceBackend.POSTGRESQL:
        return None

    @contextmanager
    def factory() -> Any:
        from zekam.infrastructure.postgres.app_server_repository import (
            PostgresAppNotificationRepository,
        )
        from zekam.infrastructure.postgres.connection import session

        with session(context.settings.database, realm_id=realm_id) as connection:
            yield PostgresAppNotificationRepository(connection, realm_id)

    return factory
