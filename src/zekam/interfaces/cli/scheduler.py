"""Run-once local maintenance scheduler; no background daemon is implied."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.mutation_admission import assert_local_effect_admission
from zekam.domain.errors import ZekamError
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.interfaces.cli import local_runtime

app = typer.Typer(name="scheduler", help="Yerel bakim run-once komutlari", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)


def _services(home: str | None) -> LocalCoreServices:
    return LocalCoreServices.from_context(build_context(home=home))


@app.command("status")
def status_command(home: Annotated[str | None, typer.Option("--home")] = None) -> None:
    """Read scheduler and composed-store status."""

    services = _services(home)
    document = {
        "schema": "zekam-local-scheduler-status/v1",
        "runtime": asdict(services.runtime.status()),
        "core": services.status(),
        "grants_authority": False,
    }
    console.print_json(json.dumps(document))


@app.command("reconcile")
def reconcile_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Run one bounded runtime recovery pass."""

    local_runtime.recover_command(apply=apply, home=home)


@app.command("rebuild")
def rebuild_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Rebuild the deterministic analytics generation from immutable segments."""

    assert_local_effect_admission(("scheduler", "rebuild"))
    services = _services(home)
    if not apply:
        console.print_json(
            json.dumps(
                {
                    "schema": "zekam-local-analytics-rebuild-plan/v1",
                    "apply": False,
                    "status": services.status(),
                }
            )
        )
        return
    try:
        result = services.analytics.rebuild(now=dt.datetime.now(dt.UTC))
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(asdict(result)))


@app.command("report")
def report_command(home: Annotated[str | None, typer.Option("--home")] = None) -> None:
    """Read and reconcile the current immutable analytics reports."""

    try:
        document = _services(home).analytics.current_projection()
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(document))
