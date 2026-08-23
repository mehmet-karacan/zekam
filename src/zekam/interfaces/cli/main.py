"""Zekam komut satiri giris noktasi.

CLI ayri urun kurali tanimlamaz; application katmanindaki servisleri cagirir.
Cikis kodlari otomasyon icin kararlidir.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Annotated

import click
import typer
from rich.console import Console
from rich.table import Table

from zekam import __version__
from zekam.application.composition import ApplicationContext, build_context, build_doctor
from zekam.application.config import USER_CONFIG_FILE, PersistenceBackend
from zekam.application.diagnostics import DoctorReport, OverallStatus, Severity
from zekam.application.home import resolve_home
from zekam.application.opencode_agent_bootstrap import (
    apply_opencode_agent_bootstrap,
    plan_opencode_agent_bootstrap,
)
from zekam.application.persistence_setup import (
    apply_persistence_setup,
    plan_persistence_setup,
)
from zekam.domain.errors import ZekamError
from zekam.domain.identity import PRODUCT
from zekam.interfaces.cli import ask as ask_commands
from zekam.interfaces.cli import backup as backup_commands
from zekam.interfaces.cli import db as db_commands
from zekam.interfaces.cli import governance as governance_commands
from zekam.interfaces.cli import jira as jira_commands
from zekam.interfaces.cli import knowledge as knowledge_commands
from zekam.interfaces.cli import model as model_commands
from zekam.interfaces.cli import opencode as opencode_commands
from zekam.interfaces.cli import oracle as oracle_commands
from zekam.interfaces.cli import project as project_commands
from zekam.interfaces.cli import sandbox as sandbox_commands
from zekam.interfaces.cli import scheduler as scheduler_commands
from zekam.interfaces.cli import surface as surface_commands
from zekam.interfaces.cli import ui as ui_commands
from zekam.interfaces.cli import work as work_commands
from zekam.interfaces.cli import worker as worker_commands

#: Toplam duruma karsilik gelen kararli cikis kodlari.
EXIT_CODES: dict[OverallStatus, int] = {
    OverallStatus.HEALTHY: 0,
    OverallStatus.DEGRADED: 1,
    OverallStatus.BLOCKED: 2,
    OverallStatus.RECOVERY_REQUIRED: 3,
}

_HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"

EXIT_USAGE_ERROR = 64
EXIT_RUNTIME_ERROR = 70

app = typer.Typer(
    name=PRODUCT.cli,
    help=f"{PRODUCT.name} - kanit tabanli calisma ve bilgi platformu",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
error_console = Console(stderr=True)

app.add_typer(db_commands.app)
app.add_typer(project_commands.app)
app.add_typer(work_commands.app)
app.add_typer(governance_commands.policy_app)
app.add_typer(governance_commands.secret_app)
app.add_typer(governance_commands.auth_app)
app.add_typer(model_commands.app)
app.add_typer(oracle_commands.app)
app.add_typer(opencode_commands.app)
app.add_typer(backup_commands.app)
app.add_typer(ask_commands.app)
app.command("ask")(ask_commands.ask_command)
app.add_typer(sandbox_commands.sandbox_app)
app.add_typer(sandbox_commands.git_app)
app.add_typer(knowledge_commands.app)
app.add_typer(jira_commands.app)
app.add_typer(scheduler_commands.scheduler_app)
app.add_typer(scheduler_commands.report_app)
app.add_typer(surface_commands.app)
app.add_typer(ui_commands.app)
app.add_typer(worker_commands.app)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"{PRODUCT.name} {__version__}")
        raise typer.Exit(0)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Surumu yazar"),
    ] = False,
) -> None:
    """Ortak secenekler."""


@app.command()
def doctor(
    output_json: Annotated[bool, typer.Option("--json", help="Raporu JSON olarak yazar")] = False,
    category: Annotated[
        list[str] | None,
        typer.Option("--category", "-c", help="Yalnizca verilen kategorileri calistirir"),
    ] = None,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Kurulum, bagimlilik ve durum butunlugunu salt okunur raporlar."""
    try:
        context = build_context(home=home)
        service = build_doctor(context)
        report = service.run(categories=category or None)
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc

    if output_json:
        console.print_json(json.dumps(report.as_dict(), ensure_ascii=False))
    else:
        _render_report(report)
    raise typer.Exit(EXIT_CODES[report.overall])


@app.command()
def init(
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Degisiklik yapmadan planlanan yerlesimi yazar")
    ] = False,
    persistence: Annotated[
        PersistenceBackend | None,
        typer.Option(
            "--persistence",
            "--veritabani",
            help="Ilk kurulum persistence motoru: postgresql veya sqlite",
        ),
    ] = None,
) -> None:
    """ZEKAM_HOME ve tek seferlik persistence secimini idempotent olarak kurar."""
    try:
        resolved_home = resolve_home(home)
        persistence = _interactive_persistence_choice(resolved_home, persistence)
        persistence_plan = plan_persistence_setup(home=resolved_home, requested=persistence)
        context = build_context(home=home)
        opencode_plan = plan_opencode_agent_bootstrap(
            executable=_opencode_executable(context), user_home=Path.home()
        )
        if dry_run:
            table = Table(title=f"{PRODUCT.data_root_env} plani: {context.home}")
            table.add_column("Dizin")
            table.add_column("Sahiplik")
            table.add_column("Durum")
            for entry in context.layout.entries():
                exists = (context.home / entry.relative).is_dir()
                table.add_row(
                    entry.relative,
                    entry.ownership.value,
                    "mevcut" if exists else "olusturulacak",
                )
            table.add_row(
                USER_CONFIG_FILE,
                "user-data",
                (
                    f"persistence={persistence_plan.backend.value}; "
                    + ("mevcut" if persistence_plan.config_exists else "olusturulacak")
                ),
            )
            if opencode_plan.available:
                table.add_row(
                    ".config/opencode/agents",
                    "user-config",
                    (
                        f"{len(opencode_plan.agents_to_create)} agent olusturulacak; "
                        f"default={opencode_plan.config_document['default_agent']}"
                    ),
                )
                table.add_row(
                    ".config/opencode/plugins/zekam-lifecycle.js",
                    "user-config",
                    ("olusturulacak" if opencode_plan.lifecycle_plugin_to_create else "mevcut"),
                )
            console.print(table)
            raise typer.Exit(0)
        context.layout.ensure()
        apply_persistence_setup(persistence_plan)
        apply_opencode_agent_bootstrap(opencode_plan)
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print(f"[green]Hazir:[/green] {context.home}")


def _opencode_executable(context: ApplicationContext) -> Path | None:
    """Config kaydi veya PATH uzerinden OpenCode'u bulur."""

    for client in context.settings.clients:
        if client.name.casefold() == "opencode":
            return client.executable
    discovered = shutil.which("opencode")
    return Path(discovered).resolve() if discovered else None


def _interactive_persistence_choice(
    home: Path, requested: PersistenceBackend | None
) -> PersistenceBackend | None:
    """Ilk interaktif kurulumda secimi bir kez sorar; otomasyonda PG varsayilani korunur."""
    if requested is not None:
        return requested
    if (home / USER_CONFIG_FILE).exists():
        existing = plan_persistence_setup(home=home, requested=None)
        if not existing.legacy_config:
            return None
    if not sys.stdin.isatty():
        return None
    raw = typer.prompt(
        "Persistence motoru",
        default=PersistenceBackend.POSTGRESQL.value,
        type=click.Choice([item.value for item in PersistenceBackend], case_sensitive=False),
    )
    return PersistenceBackend(str(raw).lower())


_SEVERITY_STYLES: dict[Severity, str] = {
    Severity.INFO: "cyan",
    Severity.WARNING: "yellow",
    Severity.ERROR: "red",
    Severity.CRITICAL: "bold red",
}

_STATUS_STYLES: dict[OverallStatus, str] = {
    OverallStatus.HEALTHY: "green",
    OverallStatus.DEGRADED: "yellow",
    OverallStatus.BLOCKED: "red",
    OverallStatus.RECOVERY_REQUIRED: "bold red",
}


def _render_report(report: DoctorReport) -> None:
    table = Table(title=f"{PRODUCT.cli} doctor")
    table.add_column("Kontrol")
    table.add_column("Durum")
    table.add_column("Ozet")
    for result in report.results:
        table.add_row(result.check_id, result.status.value, result.summary)
    console.print(table)

    if report.findings:
        console.print()
        for finding in report.findings:
            style = _SEVERITY_STYLES[finding.severity]
            console.print(f"[{style}]{finding.severity.value}[/{style}] {finding.code}")
            console.print(f"  {finding.title}: {finding.detail}")
            console.print(f"  Sonraki adim: {finding.next_action}")
            if finding.authority_required:
                console.print("  Yetki gerekir: evet")

    style = _STATUS_STYLES[report.overall]
    console.print()
    console.print(f"Toplam durum: [{style}]{report.overall.value}[/{style}]")


def run() -> None:
    """Konsol giris noktasi."""
    try:
        app()
    except ZekamError as exc:  # pragma: no cover - beklenmeyen sizinti icin son savunma
        error_console.print(f"[red]Hata:[/red] {exc}")
        sys.exit(EXIT_RUNTIME_ERROR)


if __name__ == "__main__":  # pragma: no cover
    run()
