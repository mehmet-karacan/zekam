"""`zekam backup` commands for manifests and complete local bundles."""

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
from zekam.infrastructure.local_backup import create_bundle, restore_bundle, verify_bundle
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.sqlite.operational_schema import MIGRATION_LEDGER
from zekam.infrastructure.sqlite.operational_schema import status as sqlite_status
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

EXIT_RUNTIME_ERROR = 70
EXIT_VERIFICATION_FAILED = 2

app = typer.Typer(name="backup", help="Yedek manifesti islemleri", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)

_HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"


def _schema_state(home: str | None) -> SchemaState:
    context = build_context(home=home)
    sqlite_current = sqlite_status(context.settings.database.sqlite_path(context.home))
    if not sqlite_current.integrity_ok or not sqlite_current.schema_ok:
        return schema_state_from_status(None, [])
    return schema_state_from_status(
        sqlite_current.schema_version,
        list(MIGRATION_LEDGER),
    )


def _store(home: str | None) -> LocalContentAddressedStore:
    context = build_context(home=home)
    return LocalContentAddressedStore(
        context.home / context.settings.object_store_relative
    ).ensure()


@app.command("create")
def create_command(
    output: Annotated[Path | None, typer.Option("--cikti", help="Manifest dosyasi yolu")] = None,
    bundle: Annotated[Path | None, typer.Option("--bundle", help="Yeni tam yedek dizini")] = None,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Mevcut durumdan yedek manifesti uretir."""
    assert_local_effect_admission(("backup", "create"))
    try:
        context = build_context(home=home)
        if bundle is not None:
            bundle_document = create_bundle(
                LocalCoreServices.from_context(context), context.home, bundle.absolute()
            )
            encoded = json.dumps(bundle_document, ensure_ascii=False, sort_keys=True)
            if output is None:
                console.print_json(encoded)
            else:
                output.write_bytes((bundle.absolute() / "MANIFEST.json").read_bytes())
            return
        manifest = build_manifest(
            schema_state=_schema_state(home),
            store=_store(home),
            configuration=context.settings.sanitized(),
        )
    except (OSError, ZekamError) as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc

    legacy_document = json.dumps(_encode(manifest), ensure_ascii=False, indent=2) + "\n"
    if output is None:
        console.print_json(legacy_document)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(legacy_document, encoding="utf-8", newline="\n")
    console.print(f"[green]Manifest yazildi:[/green] {output}")
    console.print(f"artifact: {len(manifest.artifacts)}, toplam bayt: {manifest.total_bytes}")


@app.command("verify")
def verify_command(
    manifest_path: Annotated[Path, typer.Argument(help="Dogrulanacak manifest dosyasi")],
    bundle: Annotated[Path | None, typer.Option("--bundle", help="Tam yedek dizini")] = None,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Manifest butunlugunu ve artifact varligini dogrular."""
    assert_local_effect_admission(("backup", "verify"))
    try:
        if bundle is not None:
            if manifest_path.read_bytes() != (bundle.absolute() / "MANIFEST.json").read_bytes():
                raise ValueError("Bundle manifest path does not match the bundle")
            document = verify_bundle(bundle.absolute())
            console.print_json(
                json.dumps(
                    {
                        "valid": True,
                        "manifest_digest": document["manifest_digest"],
                        "file_count": document["file_count"],
                    }
                )
            )
            return
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


@app.command("restore")
def restore_command(
    bundle: Annotated[Path, typer.Argument(help="Dogrulanmis tam yedek dizini")],
    target: Annotated[Path, typer.Argument(help="Yeni, mevcut olmayan hedef home")],
) -> None:
    """Verify and atomically restore a complete bundle to a new target."""

    assert_local_effect_admission(("backup", "restore"))
    try:
        receipt = restore_bundle(bundle.absolute(), target.absolute())
    except (OSError, ValueError, ZekamError) as exc:
        error_console.print(f"[red]Hata:[/red] restore basarisiz ({type(exc).__name__})")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print_json(json.dumps(receipt, ensure_ascii=False))


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
