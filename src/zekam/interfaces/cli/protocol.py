"""Exact-version, authority-free protocol generation CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.mutation_admission import assert_local_effect_admission
from zekam.domain.app_server_protocol import schema_bundle_digest
from zekam.protocol.generation import (
    generate_protocol_artifacts,
    protocol_artifact_digest,
    render_protocol_artifacts,
)

app = typer.Typer(name="protocol", help="App Server protocol SDK islemleri", no_args_is_help=True)
console = Console()


@app.command("digest")
def digest_command(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Generated protocol artifact setinin kanonik digestini yazar."""
    value = protocol_artifact_digest()
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "artifact_digest": value,
                    "schema_bundle_digest": schema_bundle_digest(),
                },
                ensure_ascii=False,
            )
        )
    else:
        console.print(value)


def _generate_one(name: str, output: Path | None) -> None:
    content = render_protocol_artifacts()[name]
    if output is None:
        console.print(content, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8", newline="\n")


@app.command("generate-json-schema")
def generate_json_schema(
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Deterministic JSON Schema'yi stdout veya exact dosyaya yazar."""
    assert_local_effect_admission(("protocol", "generate-json-schema"))
    _generate_one("schema-bundle.json", output)


@app.command("generate-typescript")
def generate_typescript(
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Deterministic TypeScript istemci tiplerini yazar."""
    assert_local_effect_admission(("protocol", "generate-typescript"))
    _generate_one("client-types.ts", output)


def check_generated(output: Path) -> bool:
    """CLI testleri icin byte-exact generated resource kontrolu."""
    return generate_protocol_artifacts(output, check=True)
