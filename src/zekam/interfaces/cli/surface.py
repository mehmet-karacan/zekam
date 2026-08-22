"""`zekam surface` komutlari.

Kanonik komut sozlesmesini gercekte kayitli CLI komutlariyla karsilastirir.
Belge ile kod arasindaki sapma burada gorunur hale gelir.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from zekam.domain.observability import (
    CANONICAL_COMMANDS,
    REQUIRED_TILES,
    missing_commands,
)

app = typer.Typer(name="surface", help="Komut yuzeyi sozlesmesi", no_args_is_help=True)
console = Console()


def registered_commands(application: typer.Typer) -> tuple[str, ...]:
    """Kayitli komutlari '<grup> <komut>' bicimde toplar."""

    found: list[str] = []
    for command in application.registered_commands:
        name = command.name or (command.callback.__name__ if command.callback else None)
        if name:
            found.append(name)
    for group in application.registered_groups:
        prefix = group.name or (group.typer_instance.info.name if group.typer_instance else None)
        instance = group.typer_instance
        if prefix is None or instance is None:
            continue
        for command in instance.registered_commands:
            name = command.name or (command.callback.__name__ if command.callback else None)
            if name:
                found.append(f"{prefix} {name}")
    return tuple(sorted(set(found)))


@app.command("contract")
def contract_command(
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Kanonik komut sozlesmesini gosterir."""
    payload: list[dict[str, Any]] = [item.as_dict() for item in CANONICAL_COMMANDS]
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title="Kanonik komut yuzeyi")
    table.add_column("Komut")
    table.add_column("Mutasyon")
    table.add_column("--uygula")
    table.add_column("Ozet")
    for item in payload:
        table.add_row(
            item["name"],
            "evet" if item["mutating"] else "hayir",
            "evet" if item["requires_apply_flag"] else "hayir",
            item["summary"],
        )
    console.print(table)


@app.command("check")
def check_command(
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Sozlesmedeki komutlarin gercekten kayitli oldugunu dogrular."""
    from zekam.interfaces.cli.main import app as root

    available = registered_commands(root)
    missing = missing_commands(available)
    payload = {
        "registered_count": len(available),
        "contract_count": len(CANONICAL_COMMANDS),
        "missing": list(missing),
        "required_tiles": list(REQUIRED_TILES),
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
    elif missing:
        console.print(f"[red]eksik komut:[/red] {', '.join(missing)}")
    else:
        console.print("[green]Butun kanonik komutlar kayitli.[/green]")
    if missing:
        raise typer.Exit(1)
