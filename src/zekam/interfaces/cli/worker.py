"""Bounded local worker composition over the canonical SQLite runtime."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.mutation_admission import assert_local_effect_admission
from zekam.interfaces.cli import local_runtime

app = typer.Typer(name="worker", help="Yerel queue worker", no_args_is_help=True)
console = Console()


@app.command("status")
def status_command(home: Annotated[str | None, typer.Option("--home")] = None) -> None:
    """Read the durable queue status without claiming work."""

    console.print_json(json.dumps(asdict(local_runtime._store(home).status())))


@app.command("run-once")
def run_once_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    owner_id: Annotated[str, typer.Option("--owner-id")] = "zekam-local-worker",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Claim and execute at most one local job, only with explicit apply."""

    assert_local_effect_admission(("worker", "run-once"))
    if not apply:
        console.print_json(
            json.dumps(
                {
                    "schema": "zekam-local-worker-plan/v1",
                    "apply": False,
                    "status": asdict(local_runtime._store(home).status()),
                }
            )
        )
        return
    local_runtime.worker_once_command(owner_id=owner_id, home=home, pause_after_effect_ms=0)


@app.command("reconcile")
def reconcile_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Run the existing bounded local recovery sweep."""

    local_runtime.recover_command(apply=apply, home=home)
