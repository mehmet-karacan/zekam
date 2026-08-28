"""Zekam komut satiri giris noktasi.

CLI ayri urun kurali tanimlamaz; application katmanindaki servisleri cagirir.
Cikis kodlari otomasyon icin kararlidir.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated
from uuid import UUID

import click
import typer
from rich.console import Console
from rich.table import Table

from zekam import __version__
from zekam.application.client_instruction_bootstrap import (
    apply_client_instruction_bootstrap,
    plan_client_instruction_bootstrap,
)
from zekam.application.composition import ApplicationContext, build_context, build_doctor
from zekam.application.config import USER_CONFIG_FILE, PersistenceBackend
from zekam.application.diagnostics import DoctorReport, OverallStatus, Severity
from zekam.application.doctor_repair import DoctorRepairPlan, build_doctor_repair_plan
from zekam.application.doctor_repair_runtime import apply_doctor_repair_with_runtime
from zekam.application.home import resolve_home
from zekam.application.mutation_admission import (
    CLI_MUTATION_REGISTRY_META_KEY,
    DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY,
)
from zekam.application.opencode_agent_bootstrap import (
    apply_opencode_agent_bootstrap,
    plan_opencode_agent_bootstrap,
)
from zekam.application.persistence_setup import (
    apply_persistence_setup,
    plan_persistence_setup,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.realm_context import RealmContext
from zekam.application.setup import build_setup_plan
from zekam.domain.errors import NotFound, PolicyViolation, ZekamError
from zekam.domain.identity import PRODUCT
from zekam.domain.realm import DEFAULT_REALM_SLUG, ActorKind, LifecycleStatus
from zekam.infrastructure.postgres.connection import connect
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.interfaces.cli import ask as ask_commands
from zekam.interfaces.cli import backup as backup_commands
from zekam.interfaces.cli import client as client_commands
from zekam.interfaces.cli import close as close_commands
from zekam.interfaces.cli import configuration as configuration_commands
from zekam.interfaces.cli import db as db_commands
from zekam.interfaces.cli import governance as governance_commands
from zekam.interfaces.cli import jira as jira_commands
from zekam.interfaces.cli import knowledge as knowledge_commands
from zekam.interfaces.cli import memory as memory_commands
from zekam.interfaces.cli import model as model_commands
from zekam.interfaces.cli import opencode as opencode_commands
from zekam.interfaces.cli import oracle as oracle_commands
from zekam.interfaces.cli import project as project_commands
from zekam.interfaces.cli import protocol as protocol_commands
from zekam.interfaces.cli import sandbox as sandbox_commands
from zekam.interfaces.cli import scheduler as scheduler_commands
from zekam.interfaces.cli import surface as surface_commands
from zekam.interfaces.cli import trace as trace_commands
from zekam.interfaces.cli import ui as ui_commands
from zekam.interfaces.cli import work as work_commands
from zekam.interfaces.cli import worker as worker_commands
from zekam.interfaces.cli.session import REALM_HELP, RealmSession

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
app.add_typer(protocol_commands.app)
app.add_typer(work_commands.app)
app.add_typer(governance_commands.policy_app)
app.add_typer(governance_commands.secret_app)
app.add_typer(governance_commands.auth_app)
app.add_typer(model_commands.app)
app.add_typer(oracle_commands.app)
app.add_typer(opencode_commands.app)
app.add_typer(backup_commands.app)
app.add_typer(client_commands.app)
app.add_typer(close_commands.app)
app.add_typer(configuration_commands.config_app)
app.add_typer(configuration_commands.permission_app)
app.add_typer(ask_commands.app)
app.command("ask")(ask_commands.ask_command)
app.add_typer(sandbox_commands.sandbox_app)
app.add_typer(sandbox_commands.git_app)
app.add_typer(knowledge_commands.app)
app.add_typer(memory_commands.app)
app.add_typer(jira_commands.app)
app.add_typer(scheduler_commands.scheduler_app)
app.add_typer(scheduler_commands.report_app)
app.add_typer(surface_commands.app)
app.add_typer(trace_commands.app)
app.add_typer(ui_commands.app)
app.add_typer(worker_commands.app)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"{PRODUCT.name} {__version__}")
        raise typer.Exit(0)


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Surumu yazar"),
    ] = False,
) -> None:
    """Ortak secenekler."""

    # Child command contexts inherit this immutable registry through Click
    # ``meta``.  The registry is authority-free; leaf services retain their
    # existing exact plan/authorization/claim/receipt gates.
    ctx.find_root().meta[CLI_MUTATION_REGISTRY_META_KEY] = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY


@app.command()
def doctor(
    output_json: Annotated[bool, typer.Option("--json", help="Raporu JSON olarak yazar")] = False,
    category: Annotated[
        list[str] | None,
        typer.Option("--category", "-c", help="Yalnizca verilen kategorileri calistirir"),
    ] = None,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
    repair_plan: Annotated[
        bool,
        typer.Option(
            "--repair-plan",
            help="Mutation yapmadan digest-bound Git/DB onarim planini yazar",
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option(
            "--uygula",
            help="Exact --plan-digest ile siradaki tek onarim adimini uygular",
        ),
    ] = False,
    plan_digest: Annotated[
        str | None,
        typer.Option("--plan-digest", help="Uygulanacak exact doctor repair plan digest'i"),
    ] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    project_id: Annotated[
        UUID | None,
        typer.Option("--project-id", help="Exact Zekam source project UUID"),
    ] = None,
    actor_id: Annotated[
        UUID | None,
        typer.Option("--actor-id", help="Exact aktif human actor UUID"),
    ] = None,
) -> None:
    """Kurulum ve butunlugu raporlar; mutation yalniz explicit repair ile olur."""
    try:
        context = build_context(home=home)
        service = build_doctor(context)
        report = service.run(categories=category or None)
        selected_plan: DoctorRepairPlan | None = None
        applied_result: dict[str, object] | None = None
        if repair_plan or apply:
            if context.settings.database.backend is PersistenceBackend.POSTGRESQL:
                with connect(context.settings.database) as connection:
                    selected_plan = build_doctor_repair_plan(
                        core_path=context.core_path,
                        connection=connection,
                        migrations_directory=context.core_path / "migrations",
                    )
            else:
                selected_plan = build_doctor_repair_plan(core_path=context.core_path)
        if apply:
            if plan_digest is None:
                raise PolicyViolation("--uygula exact --plan-digest ister")
            if context.settings.database.backend is not PersistenceBackend.POSTGRESQL:
                raise PolicyViolation("Doctor repair runtime PostgreSQL Work Graph ister")
            assert selected_plan is not None
            if selected_plan.plan_digest != plan_digest:
                raise PolicyViolation("Doctor repair plan digest stale veya exact degil")
            with RealmSession(home, realm) as realm_context:
                exact_project_id = _doctor_project_id(
                    realm_context,
                    context,
                    requested=project_id,
                )
                exact_actor_id = _doctor_actor_id(realm_context, requested=actor_id)
                runtime_result = apply_doctor_repair_with_runtime(
                    realm_context,
                    context,
                    repair_plan=selected_plan,
                    plan_digest=plan_digest,
                    actor_id=exact_actor_id,
                    project_id=exact_project_id,
                )
                applied_result = runtime_result.as_dict()
            report = build_doctor(context).run(categories=category or None)
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc

    if output_json:
        document = report.as_dict()
        if selected_plan is not None:
            document["doctor_repair_plan"] = selected_plan.as_dict()
        if applied_result is not None:
            document["doctor_repair_result"] = applied_result
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        _render_report(report)
        if selected_plan is not None:
            _render_doctor_repair_plan(selected_plan)
        if applied_result is not None:
            console.print(
                "[green]Onarim dogrulandi:[/green] "
                f"{applied_result['step']} receipt={applied_result['receipt_id']}"
            )
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
        client_instruction_plan = plan_client_instruction_bootstrap(user_home=Path.home())
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
            for instruction in client_instruction_plan.files:
                table.add_row(
                    str(instruction.path.relative_to(Path.home())),
                    "user-config",
                    instruction.action,
                )
            console.print(table)
            raise typer.Exit(0)
        context.layout.ensure()
        apply_persistence_setup(persistence_plan)
        apply_opencode_agent_bootstrap(opencode_plan)
        apply_client_instruction_bootstrap(client_instruction_plan)
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print(f"[green]Hazir:[/green] {context.home}")


@app.command()
def setup(
    apply: Annotated[
        bool,
        typer.Option(
            "--uygula",
            help="Git TLS, migration ve runtime ilk kurulum adimlarini gercekten uygular",
        ),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Sanitize plan JSON yazar")] = False,
) -> None:
    """Yeni makine kurulumunu planlar; varsayilan davranis salt okunur dry-run'dir."""

    plan = build_setup_plan()
    if output_json and not apply:
        console.print_json(
            json.dumps(
                {
                    "schema": "zekam-setup-plan/v1",
                    "apply": apply,
                    "steps": [step.as_dict() for step in plan],
                },
                ensure_ascii=False,
            )
        )
        return
    elif not apply:
        table = Table(title="Yeni makine kurulum plani")
        table.add_column("Adim")
        table.add_column("Aciklama")
        for step in plan:
            table.add_row(step.step_id, step.description)
        console.print(table)
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return

    cli_prefix = (sys.executable, "-m", "zekam.interfaces.cli.main")
    receipts: list[dict[str, object]] = []
    for step in plan:
        argv = step.argv if step.step_id == "windows-git-ca" else (*cli_prefix, *step.argv)
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=output_json,
            text=output_json,
        )
        receipts.append({"step_id": step.step_id, "returncode": completed.returncode})
        if completed.returncode != 0:
            if output_json:
                console.print_json(
                    json.dumps(
                        {
                            "schema": "zekam-setup-result/v1",
                            "status": "failed",
                            "receipts": receipts,
                        }
                    )
                )
            else:
                error_console.print(f"[red]Kurulum durdu:[/red] {step.step_id}")
            raise typer.Exit(completed.returncode)
        if not output_json:
            console.print(f"[green]Tamam:[/green] {step.step_id}")
    if output_json:
        console.print_json(
            json.dumps(
                {
                    "schema": "zekam-setup-result/v1",
                    "status": "completed",
                    "receipts": receipts,
                }
            )
        )


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


def _doctor_project_id(
    realm_context: RealmContext,
    context: ApplicationContext,
    *,
    requested: UUID | None,
) -> UUID:
    integration = ProjectIntegrationService(realm_context.connection, realm_context.realm)
    if requested is not None:
        integration.projects.get(requested)
        if integration.resolve_source_root(requested).resolve() != context.core_path.resolve():
            raise PolicyViolation("--project-id exact Zekam source rootuna bagli degil")
        return requested
    candidates: list[UUID] = []
    for project in integration.projects.list_all():
        try:
            root = integration.resolve_source_root(project.id)
        except (NotFound, PolicyViolation):
            continue
        if root.resolve() == context.core_path.resolve():
            candidates.append(project.id)
    if len(candidates) != 1:
        raise PolicyViolation(
            "Doctor repair exact tek Zekam source project ister; --project-id verin"
        )
    return candidates[0]


def _doctor_actor_id(realm_context: RealmContext, *, requested: UUID | None) -> UUID:
    actors = ActorRepository(realm_context.connection, realm_context.realm_id)
    if requested is not None:
        actor = actors.get(requested)
        if actor.kind is not ActorKind.HUMAN or actor.status is not LifecycleStatus.ACTIVE:
            raise PolicyViolation("--actor-id aktif human actor olmali")
        return actor.id
    candidates = tuple(
        actor
        for actor in actors.list_all()
        if actor.kind is ActorKind.HUMAN and actor.status is LifecycleStatus.ACTIVE
    )
    if len(candidates) != 1:
        raise PolicyViolation("Doctor repair exact tek aktif human actor ister; --actor-id verin")
    return candidates[0].id


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


def _render_doctor_repair_plan(plan: DoctorRepairPlan) -> None:
    document = plan.as_dict()
    table = Table(title="Doctor repair plani (yetki degildir)")
    table.add_column("Alan")
    table.add_column("Deger")
    table.add_row("Plan digest", plan.plan_digest)
    table.add_row("Siradaki adim", str(document["next_step"] or "yok"))
    table.add_row(
        "Bloke",
        ", ".join(str(item) for item in document["blocked_reasons"]) or "hayir",
    )
    table.add_row("Uygulanabilir", "evet" if document["applicable"] else "hayir")
    console.print()
    console.print(table)
    if document["next_step"] is not None:
        console.print(
            "Uygulamak icin exact plan digest ile tekrar calistirin: "
            f"`{PRODUCT.cli} doctor --uygula --plan-digest {plan.plan_digest}`"
        )


def run() -> None:
    """Konsol giris noktasi."""
    try:
        app()
    except ZekamError as exc:  # pragma: no cover - beklenmeyen sizinti icin son savunma
        error_console.print(f"[red]Hata:[/red] {exc}")
        sys.exit(EXIT_RUNTIME_ERROR)


if __name__ == "__main__":  # pragma: no cover
    run()
