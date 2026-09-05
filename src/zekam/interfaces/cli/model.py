"""Provider-free local model compatibility commands."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.model_benchmark_service import load_fixture_registry

app = typer.Typer(name="model", help="Yerel model kanit ve benchmark yuzeyi")
console = Console()
error_console = Console(stderr=True)


@app.command("benchmark")
def benchmark_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    model: Annotated[str | None, typer.Option("--model")] = None,
    inventory_digest: Annotated[str | None, typer.Option("--inventory-digest")] = None,
    policy_digest: Annotated[str | None, typer.Option("--policy-digest")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
) -> None:
    if apply or any(value is not None for value in (model, inventory_digest, policy_digest)):
        error_console.print("Benchmark apply exact authorization runtime gate ister")
        raise typer.Exit(6)
    registry = load_fixture_registry()
    document = {
        "schema": "zekam-local-benchmark-catalog/v1",
        "fixture_count": len(registry.fixtures),
        "local_fixture_count": len(registry.eligible(remote=False)),
        "remote_fixture_count": len(registry.eligible(remote=True)),
        "registry_digest": registry.registry_digest,
        "provider_calls": 0,
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, sort_keys=True))
        return
    console.print(f"fixtures={len(registry.fixtures)} provider_calls=0")


@app.command("decide")
def decide_command(
    input_path: Annotated[str, typer.Option("--girdi")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    del input_path, output_json
    error_console.print("Caller-supplied model candidates cannot satisfy authoritative hard gate")
    raise typer.Exit(6)


@app.command("health")
def health_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
) -> None:
    if apply:
        error_console.print(
            "Production health sentetik probe ile yazilamaz; exact authorization gerekir"
        )
        raise typer.Exit(6)
    console.print("Dry-run; provider call yok")
