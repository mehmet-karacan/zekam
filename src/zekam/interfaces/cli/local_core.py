"""Read-only status surface for the composed local WP00-WP14 stores."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.domain.errors import ZekamError
from zekam.domain.identity import PRODUCT
from zekam.infrastructure.local_core_services import LocalCoreServices

app = typer.Typer(name="local-core", help="Yerel WP00-WP14 servisleri", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)
HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"


@app.command("status")
def status_command(
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Operational, learning, model, benchmark, routing and analytics state."""

    try:
        document = LocalCoreServices.from_context(build_context(home=home)).status(
            semantic_analytics=True
        )
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(document, ensure_ascii=False))
    if not document["all_ready"]:
        raise typer.Exit(2)
