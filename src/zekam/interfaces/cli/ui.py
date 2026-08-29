"""Local read-only web UI commands."""

from __future__ import annotations

import ipaddress
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.domain.errors import ZekamError

app = typer.Typer(
    name="ui",
    help="Salt okunur Canli Yurutme Gozleme Merkezi",
    no_args_is_help=True,
)
console = Console()
error_console = Console(stderr=True)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def validate_lan_bind_host(host: str) -> str:
    """Require one exact non-loopback interface IP; never accept wildcards."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("LAN host exact IP olmali") from exc
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError("LAN host belirli bir non-loopback IP olmali")
    return str(address)


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
        typer.Option(help="Varsayilan olarak yalniz loopback arayuzune baglanir"),
    ] = "127.0.0.1",
    allow_lan: Annotated[
        bool,
        typer.Option(
            "--allow-lan",
            help="Acik yetkiyle belirtilen LAN adresine baglanmaya izin verir",
        ),
    ] = False,
    port: Annotated[
        int,
        typer.Option(min=1, max=65535, help="Yerel HTTP portu"),
    ] = 8765,
    refresh_ms: Annotated[
        int,
        typer.Option(min=500, max=30000, help="Projeksiyon yenileme araligi"),
    ] = 2000,
) -> None:
    """Zekam'in salt okunur canli yurutme gozlem arayuzunu baslatir."""

    lan_host: str | None = None
    if host not in _LOOPBACK_HOSTS:
        if not allow_lan:
            error_console.print("[red]Hata:[/red] LAN adresi icin acik --allow-lan yetkisi gerekli")
            raise typer.Exit(64)
        try:
            lan_host = validate_lan_bind_host(host)
        except ValueError as exc:
            error_console.print(f"[red]Hata:[/red] {exc}")
            raise typer.Exit(64) from exc
    try:
        resolved_realm = UUID(realm_id) if realm_id is not None else None
        context = build_context(home=home)
        from zekam.interfaces.api.observatory import create_app

        web_app = create_app(
            context,
            realm_id=resolved_realm,
            refresh_seconds=refresh_ms / 1000,
            allowed_hosts=() if lan_host is None else (lan_host,),
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
    console.print(
        "[green]Zekam Canli Yurutme Gozleme Merkezi:[/green] "
        f"http://{display_host}:{port} ({mode})"
    )
    uvicorn.run(web_app, host=host, port=port, access_log=False, log_level="warning")
