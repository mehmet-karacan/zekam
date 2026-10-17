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
    missing_commands,
)

app = typer.Typer(name="surface", help="Komut yuzeyi sozlesmesi", no_args_is_help=True)
console = Console()


def registered_commands(application: typer.Typer) -> tuple[str, ...]:
    """Kayitli komutlari '<grup> <komut>' bicimde toplar.

    Typer nested group'lari tek seviyeli bir liste olarak kaydeder. ``git
    publication`` gibi iki seviyeli yuzeylerde onceki tek-seviye tarama,
    publication komutlarini sozlesmede yokmus gibi raporluyordu. Agaci
    recursively gezerek her komutun canonical yolunu toplariz.
    """

    found: list[str] = []

    def walk(instance: typer.Typer, prefix: tuple[str, ...] = ()) -> None:
        for command in instance.registered_commands:
            name = command.name or (command.callback.__name__ if command.callback else None)
            if name:
                found.append(" ".join((*prefix, name)))
        for group in instance.registered_groups:
            child = group.typer_instance
            name = group.name or (child.info.name if child else None)
            if child is None or name is None:
                continue
            walk(child, (*prefix, name))

    walk(application)
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
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
    elif missing:
        console.print(f"[red]eksik komut:[/red] {', '.join(missing)}")
    else:
        console.print("[green]Butun kanonik komutlar kayitli.[/green]")
    if missing:
        raise typer.Exit(1)
