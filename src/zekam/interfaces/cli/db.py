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
from zekam.application.unit_test_migration import UnitTestLedgerMigrationAdmission
from zekam.application.evolution_bootstrap import _spool_targets
from zekam.domain.canonical import digest
from zekam.domain.errors import ZekamError
from zekam.domain.identity import PRODUCT
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.infrastructure.sqlite.operational_migration import migrate_v5_to_v6
from zekam.infrastructure.local_file_security import restrict_private_tree
from zekam.infrastructure.sqlite import operational_schema as sqlite_repository

EXIT_RUNTIME_ERROR = 70
EXIT_DRIFT = 2

app = typer.Typer(name="db", help="Kanonik veritabani migration islemleri", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)

_HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"


def _expected_head() -> int:
    return max(sqlite_repository.RUNTIME_SCHEMA_VERSIONS)


def _migration_label(current: int | None) -> str:
    expected = _expected_head()
    if current == 5 and expected == 6:
        return "v5 -> v6 (operational-unit-test-engineering-v6)"
    return f"v{current} -> v{expected}"


def _unit_test_migration_plan(context, path) -> dict[str, object]:
    source_digest = logical_database_digest(path)
    backup = context.home / "backups" / f"operational-v5-before-v6-{source_digest[7:19]}.db"
    body: dict[str, object] = {
        "schema": "zekam-operational-unit-test-migration-plan/v1",
        "source_version": 5,
        "target_version": 6,
        "database_ref": "ZEKAM_HOME/state/operational.db",
        "backup_ref": f"ZEKAM_HOME/backups/{backup.name}",
        "migration_lock_ref": "ZEKAM_HOME/runtime/operational-v6-migration.lock",
        "source_logical_digest": source_digest,
        "spool_target_count": len(_spool_targets(path, context.home)),
        "control_state": "paused-or-disabled-required",
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    return body | {"plan_digest": digest(body)}


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
            "expected_head": _expected_head(),
            "supported_heads": sorted(sqlite_repository.RUNTIME_SCHEMA_VERSIONS),
            "integrity_ok": sqlite_status.integrity_ok,
            "schema_ok": sqlite_status.schema_ok,
            "drift": (
                []
                if sqlite_status.integrity_ok
                and sqlite_status.schema_ok
                and sqlite_status.schema_version in sqlite_repository.RUNTIME_SCHEMA_VERSIONS
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
            and current_sqlite.schema_version == _expected_head()
        ):
            console.print("[green]Bekleyen migration yok.[/green]")
            return
        console.print(f"uygulanacak: {_migration_label(current_sqlite.schema_version)}")
        console.print(
            "Bu migration explicit operational migration orchestrator ve owner admission ister."
        )
        return
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc


@app.command("upgrade")
def upgrade_command(
    apply: Annotated[
        bool, typer.Option("--uygula", help="Gercekten uygular; verilmezse yalniz plan yazilir")
    ] = False,
    plan_digest: Annotated[
        str | None,
        typer.Option("--plan-digest", help="Dry-run planinin exact digest'i"),
    ] = None,
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
            and current_sqlite.schema_version == _expected_head()
        ):
            console.print("[green]Bekleyen migration yok.[/green]")
            return
        if not apply:
            if current_sqlite.schema_version == 5 and _expected_head() == 6:
                plan = _unit_test_migration_plan(context, path)
                console.print_json(json.dumps(plan, ensure_ascii=False))
            else:
                console.print(f"uygulanacak: {_migration_label(current_sqlite.schema_version)}")
                console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
            return
        if current_sqlite.schema_version != 5 or _expected_head() != 6:
            raise ZekamError(
                "Yalniz exact operational v5 -> v6 unit-test migration CLI'dan uygulanabilir"
            )
        plan = _unit_test_migration_plan(context, path)
        if plan_digest != plan["plan_digest"]:
            raise ZekamError("Exact migration plan digest gerekli")
        backup = context.home / "backups" / str(plan["backup_ref"]).split("/")[-1]
        backup.parent.mkdir(parents=True, exist_ok=True)
        restrict_private_tree(backup.parent)
        receipt = migrate_v5_to_v6(
            path,
            backup,
            migration_lock=context.home / "runtime" / "operational-v6-migration.lock",
            admission=UnitTestLedgerMigrationAdmission(context),
            spool_targets=_spool_targets(path, context.home),
        )
        console.print_json(
            json.dumps(
                {
                    "schema": "zekam-operational-unit-test-migration-receipt/v1",
                    "plan_digest": plan["plan_digest"],
                    "source_version": receipt.source_version,
                    "target_version": receipt.status.schema_version,
                    "schema_ok": receipt.status.schema_ok,
                    "integrity_ok": receipt.status.integrity_ok,
                    "backup_ref": f"ZEKAM_HOME/backups/{backup.name}",
                    "source_logical_digest": receipt.source_v3_logical_digest,
                    "provider_calls": 0,
                    "network_calls": 0,
                    "grants_authority": False,
                },
                ensure_ascii=False,
            )
        )
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
