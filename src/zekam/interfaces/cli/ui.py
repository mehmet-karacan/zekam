"""Local read-only web UI commands."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.domain.errors import ZekamError

app = typer.Typer(name="ui", help="Salt okunur Neuro Observatory", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@app.command("serve")
def serve_command(
    realm_id: Annotated[
        str | None,
        typer.Option(
            "--realm-id",
            help="Canli PostgreSQL projeksiyonu icin exact realm UUID; yoksa belge modu",
        ),
    ] = None,
    home: Annotated[
        str | None,
        typer.Option("--home", help="ZEKAM_HOME kokunu gecici olarak ezer"),
    ] = None,
    host: Annotated[
        str,
        typer.Option(help="Ilk surum yalniz loopback arayuzune baglanir"),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(min=1, max=65535, help="Yerel HTTP portu"),
    ] = 8765,
    refresh_ms: Annotated[
        int,
        typer.Option(min=500, max=30000, help="Projeksiyon yenileme araligi"),
    ] = 2000,
) -> None:
    """Zekam'in read-only beyin/sinaps gozlem arayuzunu baslatir."""

    if host not in _LOOPBACK_HOSTS:
        error_console.print(
            "[red]Hata:[/red] UI ilk dilimde yalniz 127.0.0.1, ::1 veya localhost'a baglanir"
        )
        raise typer.Exit(64)
    try:
        resolved_realm = UUID(realm_id) if realm_id is not None else None
        context = build_context(home=home)
        from zekam.interfaces.api.observatory import create_app

        web_app = create_app(
            context,
            realm_id=resolved_realm,
            refresh_seconds=refresh_ms / 1000,
        )
        import uvicorn
    except (ValueError, RuntimeError, ZekamError) as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    except ImportError as exc:  # pragma: no cover - environment dependent
        error_console.print("[red]Hata:[/red] UI icin `pip install 'zekam[api]'` gerekli")
        raise typer.Exit(70) from exc

    mode = "canli realm" if resolved_realm is not None else "belge grafigi"
    display_host = f"[{host}]" if ":" in host else host
    console.print(f"[green]Zekam Neuro Observatory:[/green] http://{display_host}:{port} ({mode})")
    uvicorn.run(web_app, host=host, port=port, access_log=False, log_level="warning")
