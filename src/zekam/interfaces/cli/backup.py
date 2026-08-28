"""`zekam backup` komutlari: yedek manifesti uretimi ve dogrulamasi.

Her iki komut da salt okunurdur; veri kopyalamaz veya silmez. Gercek arsiv
uretimi ve restore, faz 17 kapsaminda exact authorization ile eklenir.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.backup import (
    BACKUP_MANIFEST_SCHEMA,
    ArtifactEntry,
    BackupManifest,
    SchemaState,
    build_manifest,
    schema_state_from_status,
    verify_manifest,
)
from zekam.application.composition import build_context
from zekam.application.mutation_admission import assert_local_effect_admission
from zekam.domain.errors import ZekamError
from zekam.domain.identity import PRODUCT
from zekam.infrastructure.postgres import migrations
from zekam.infrastructure.postgres.connection import PSYCOPG_AVAILABLE, connect
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

EXIT_RUNTIME_ERROR = 70
EXIT_VERIFICATION_FAILED = 2

app = typer.Typer(name="backup", help="Yedek manifesti islemleri", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)

_HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"


def _schema_state(home: str | None) -> SchemaState:
    context = build_context(home=home)
    if not PSYCOPG_AVAILABLE:
        return schema_state_from_status(None, [])
    try:
        with connect(context.settings.database) as connection:
            current = migrations.status(connection)
    except Exception:
        return schema_state_from_status(None, [])
    return schema_state_from_status(
        current.head,
        [(record.version, record.name, record.checksum) for record in current.applied],
    )


def _store(home: str | None) -> LocalContentAddressedStore:
    context = build_context(home=home)
    return LocalContentAddressedStore(
        context.home / context.settings.object_store_relative
    ).ensure()


@app.command("create")
def create_command(
    output: Annotated[Path | None, typer.Option("--cikti", help="Manifest dosyasi yolu")] = None,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Mevcut durumdan yedek manifesti uretir."""
    assert_local_effect_admission(("backup", "create"))
    try:
        context = build_context(home=home)
        manifest = build_manifest(
            schema_state=_schema_state(home),
            store=_store(home),
            configuration=context.settings.sanitized(),
        )
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc

    document = json.dumps(_encode(manifest), ensure_ascii=False, indent=2) + "\n"
    if output is None:
        console.print_json(document)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8", newline="\n")
    console.print(f"[green]Manifest yazildi:[/green] {output}")
    console.print(f"artifact: {len(manifest.artifacts)}, toplam bayt: {manifest.total_bytes}")


@app.command("verify")
def verify_command(
    manifest_path: Annotated[Path, typer.Argument(help="Dogrulanacak manifest dosyasi")],
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Manifest butunlugunu ve artifact varligini dogrular."""
    assert_local_effect_admission(("backup", "verify"))
    try:
        manifest = _decode(json.loads(manifest_path.read_text(encoding="utf-8")))
        result = verify_manifest(manifest, _store(home))
    except (OSError, KeyError, ValueError) as exc:
        error_console.print(f"[red]Hata:[/red] manifest okunamadi ({type(exc).__name__})")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc

    console.print_json(json.dumps(result.as_dict(), ensure_ascii=False))
    if not result.is_valid:
        raise typer.Exit(EXIT_VERIFICATION_FAILED)


def _encode(manifest: BackupManifest) -> dict[str, object]:
    document = manifest.as_dict()
    document["created_at"] = manifest.created_at.isoformat()
    return document


def _decode(document: dict[str, object]) -> BackupManifest:
    import datetime as dt

    if document.get("schema") != BACKUP_MANIFEST_SCHEMA:
        raise ValueError("Desteklenmeyen manifest semasi")
    state = document["schema_state"]
    assert isinstance(state, dict)
    artifacts = document["artifacts"]
    assert isinstance(artifacts, list)
    return BackupManifest(
        schema=str(document["schema"]),
        product=str(document["product"]),
        product_version=str(document["product_version"]),
        created_at=dt.datetime.fromisoformat(str(document["created_at"])),
        schema_state=SchemaState(
            head=state["head"],
            migrations=tuple(
                (item["version"], item["name"], item["checksum"]) for item in state["migrations"]
            ),
        ),
        artifacts=tuple(
            ArtifactEntry(
                digest=item["digest"],
                size_bytes=int(item["size_bytes"]),
                media_type=item.get("media_type"),
            )
            for item in artifacts
        ),
        configuration_digest=str(document["configuration_digest"]),
        manifest_digest=str(document["manifest_digest"]),
    )
