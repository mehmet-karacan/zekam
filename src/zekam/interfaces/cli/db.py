"""`zekam db` komutlari: migration durumu, plan ve uygulama.

`plan` ve `status` salt okunurdur. `upgrade` varsayilan olarak dry-run'dir; gercek
uygulama `--uygula` bayragini ister. Faz 4'te bu bayrak exact authorization
kaydiyla degistirilecektir.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.composition import build_context
from zekam.domain.errors import ZekamError
from zekam.domain.identity import PRODUCT
from zekam.infrastructure.sqlite import operational_schema as sqlite_repository

EXIT_RUNTIME_ERROR = 70
EXIT_DRIFT = 2

app = typer.Typer(name="db", help="Kanonik veritabani migration islemleri", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)

_HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"


@app.command("status")
def status_command(
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Uygulanmis head, bekleyen migration ve drift durumunu yazar."""
    try:
        context = build_context(home=home)
        sqlite_status = sqlite_repository.status(
            context.settings.database.sqlite_path(context.home)
        )
        document = {
            "backend": "sqlite",
            "head": sqlite_status.schema_version,
            "expected_head": sqlite_repository.SCHEMA_VERSION,
            "integrity_ok": sqlite_status.integrity_ok,
            "schema_ok": sqlite_status.schema_ok,
            "drift": (
                []
                if sqlite_status.integrity_ok
                and sqlite_status.schema_ok
                and sqlite_status.schema_version == sqlite_repository.SCHEMA_VERSION
                else ["sqlite-integrity-or-schema-drift"]
            ),
        }
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc

    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        table = Table(title="SQLite migration durumu")
        table.add_column("Alan")
        table.add_column("Deger")
        for key, value in document.items():
            table.add_row(key, str(value))
        console.print(table)
    if document["drift"]:
        raise typer.Exit(EXIT_DRIFT)


@app.command("plan")
def plan_command(
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Uygulanacak migration'lari ve geri alma dosyasi durumunu listeler."""
    try:
        context = build_context(home=home)
        current_sqlite = sqlite_repository.status(
            context.settings.database.sqlite_path(context.home)
        )
        if (
            current_sqlite.integrity_ok
            and current_sqlite.schema_ok
            and current_sqlite.schema_version == sqlite_repository.SCHEMA_VERSION
        ):
            console.print("[green]Bekleyen migration yok.[/green]")
            return
        console.print("uygulanacak: 0001_operational_authority")
        return
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc


@app.command("upgrade")
def upgrade_command(
    apply: Annotated[
        bool, typer.Option("--uygula", help="Gercekten uygular; verilmezse yalniz plan yazilir")
    ] = False,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Bekleyen migration'lari uygular. Varsayilan davranis dry-run'dir."""
    try:
        context = build_context(home=home)
        path = context.settings.database.sqlite_path(context.home)
        current_sqlite = sqlite_repository.status(path)
        if (
            current_sqlite.integrity_ok
            and current_sqlite.schema_ok
            and current_sqlite.schema_version == sqlite_repository.SCHEMA_VERSION
        ):
            console.print("[green]Bekleyen migration yok.[/green]")
            return
        if not apply:
            console.print("uygulanacak: 0001_operational_authority")
            console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
            return
        sqlite_repository.bootstrap(path)
        console.print("[green]uygulandi:[/green] 0001_operational_authority")
        return
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
