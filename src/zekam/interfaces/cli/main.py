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

import typer
from rich.console import Console
from rich.table import Table

from zekam import __version__
from zekam.application.active_task_contract import ActiveTaskContract
from zekam.application.capability_inventory import capability_inventory
from zekam.application.composition import ApplicationContext, build_context, build_doctor
from zekam.application.config import USER_CONFIG_FILE, PersistenceBackend
from zekam.application.diagnostics import DoctorReport, OverallStatus, Severity
from zekam.application.fresh_bootstrap import apply_fresh_bootstrap, plan_fresh_bootstrap
from zekam.application.home import resolve_home
from zekam.application.mutation_admission import (
    CLI_MUTATION_REGISTRY_META_KEY,
    DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY,
)
from zekam.application.opencode_embedding import default_opencode_config_file
from zekam.application.project_rag_runtime import (
    query_registered_project,
    resolve_question_project,
    resolve_registered_project,
)
from zekam.application.setup import build_setup_plan, setup_plan_digest, setup_plan_payload
from zekam.application.workspace_resume import build_resume_packet, render_resume_prompt
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.identity import PRODUCT
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.sqlite.operational_schema import SQLiteOperationalSchema
from zekam.interfaces.cli import backup as backup_commands
from zekam.interfaces.cli import client as client_commands
from zekam.interfaces.cli import configuration as configuration_commands
from zekam.interfaces.cli import continuity as continuity_commands
from zekam.interfaces.cli import db as db_commands
from zekam.interfaces.cli import jira as jira_commands
from zekam.interfaces.cli import knowledge as knowledge_commands
from zekam.interfaces.cli import local_core as local_core_commands
from zekam.interfaces.cli import local_runtime as local_runtime_commands
from zekam.interfaces.cli import model as model_commands
from zekam.interfaces.cli import opencode as opencode_commands
from zekam.interfaces.cli import project as project_commands
from zekam.interfaces.cli import protocol as protocol_commands
from zekam.interfaces.cli import research as research_commands
from zekam.interfaces.cli import route as route_commands
from zekam.interfaces.cli import sandbox as sandbox_commands
from zekam.interfaces.cli import scheduler as scheduler_commands
from zekam.interfaces.cli import surface as surface_commands
from zekam.interfaces.cli import ui as ui_commands
from zekam.interfaces.cli import work as work_commands
from zekam.interfaces.cli import worker as worker_commands
from zekam.interfaces.cli.session import REALM_HELP

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
_OPERATIONAL_SCHEMA = SQLiteOperationalSchema()
_DEFAULT_OPENCODE_CONFIG_FILE = default_opencode_config_file()

app.add_typer(db_commands.app)
app.add_typer(project_commands.app)
app.add_typer(protocol_commands.app)
app.add_typer(work_commands.app)
app.add_typer(model_commands.app)
app.add_typer(opencode_commands.app)
app.add_typer(backup_commands.app)
app.add_typer(client_commands.app)
app.add_typer(sandbox_commands.sandbox_app)
app.add_typer(sandbox_commands.git_app)
app.add_typer(knowledge_commands.app)
app.add_typer(local_runtime_commands.app)
app.add_typer(local_core_commands.app)
app.add_typer(continuity_commands.app)
app.add_typer(configuration_commands.config_app)
app.add_typer(configuration_commands.permission_app)
app.add_typer(jira_commands.app)
app.add_typer(route_commands.app)
app.add_typer(research_commands.app)
app.add_typer(surface_commands.app)
app.add_typer(ui_commands.app)
app.add_typer(worker_commands.app)
app.add_typer(scheduler_commands.app)


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


@app.command("capabilities")
def capabilities_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reviewed Zekam capability inventory with explicit readiness gaps."""

    document = capability_inventory()
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
        return
    counts = document["counts"]
    console.print(
        f"ready={counts['ready']} partial={counts['partial']} scaffold={counts['scaffold']}"
    )


@app.command("resume")
def resume_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    prompt: Annotated[bool, typer.Option("--prompt")] = False,
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Model-independent work, checkpoint, project and capability resume packet."""

    if output_json and prompt:
        error_console.print("[red]Hata:[/red] --json ve --prompt birlikte kullanilamaz")
        raise typer.Exit(EXIT_USAGE_ERROR)
    try:
        document = build_resume_packet(resolve_home(home), session_id=session_id)
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    if prompt:
        typer.echo(render_resume_prompt(document))
    elif output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        checkpoint = document["latest_semantic_checkpoint"]
        completed = checkpoint["completed"] if checkpoint else "semantic checkpoint yok"
        console.print(f"{document['semantic_state']}: {completed}")
        console.print(f"siradaki: {document['next_safe_action']}")


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Proje bilgisi icin exact kullanici sorusu")],
    project: Annotated[str | None, typer.Option("--project", help="Exact proje slug")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="RAG sonucunu JSON yazar")] = False,
    authorize_remote_query: Annotated[
        bool,
        typer.Option(
            "--authorize-remote-query", help="Sorgu embedding'ini uzak saglayiciya yollar"
        ),
    ] = False,
    opencode_config: Annotated[
        Path, typer.Option("--opencode-config")
    ] = _DEFAULT_OPENCODE_CONFIG_FILE,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Dogal dil sorusunu aktif project-scoped hybrid RAG indeksine yonlendirir."""

    if not authorize_remote_query:
        error_console.print(
            "[red]Hata:[/red] Remote query embedding explicit --authorize-remote-query ister"
        )
        raise typer.Exit(77)
    try:
        resolved_home = resolve_home(home).resolve(strict=True)
        selected_project = (
            resolve_registered_project(resolved_home, project)
            if project is not None
            else resolve_question_project(resolved_home, question)
        )
        retrieval = query_registered_project(
            resolved_home,
            selected_project,
            question,
            opencode_config=opencode_config,
        )
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    result = {
        "schema": "zekam-ask-result/v1",
        "project_ref": selected_project,
        "retrieval": retrieval,
    }
    if output_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
    else:
        console.print(str(retrieval.get("answer_excerpt", "")))


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
    prepare: Annotated[
        bool,
        typer.Option(
            "--hazirla",
            help="Git sonrasi pending migration ve routine onarimlarini bounded uygular",
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
        local_services: LocalCoreServices | None = None
        selected_plan: dict[str, object] | None = None
        applied_result: dict[str, object] | None = None
        if prepare and (apply or plan_digest is not None):
            raise PolicyViolation("--hazirla, --uygula veya --plan-digest ile birlikte kullanilmaz")
        if repair_plan or apply or prepare:
            local_services = LocalCoreServices.from_context(context)
            selected_plan = local_services.repair_plan()
        automatic_results: list[dict[str, object]] = []
        if prepare:
            assert selected_plan is not None and local_services is not None
            if selected_plan["action"] is not None:
                automatic_results.append(
                    local_services.apply_repair(str(selected_plan["plan_digest"]))
                )
                report = service.run(categories=category or None)
        elif apply:
            assert local_services is not None
            if plan_digest is None:
                raise PolicyViolation("--uygula exact --plan-digest ister")
            applied_result = local_services.apply_repair(plan_digest)
            report = service.run(categories=category or None)
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc

    if output_json:
        document = report.as_dict()
        if selected_plan is not None:
            document["doctor_repair_plan"] = selected_plan
        if applied_result is not None:
            document["doctor_repair_result"] = applied_result
        if automatic_results:
            document["doctor_prepare_results"] = automatic_results
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
        for item in automatic_results:
            console.print(
                "[green]Hazirlama adimi dogrulandi:[/green] "
                f"{item['step']} receipt={item['receipt_id']}"
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
            help="Fresh bootstrap motoru: sqlite",
        ),
    ] = None,
) -> None:
    """ZEKAM_HOME v2'yi sifir verili ve atomik olarak kurar."""
    del persistence  # Kept as a backwards-compatible, SQLite-only CLI option.
    try:
        resolved_home = resolve_home(home)
        context = build_context(home=home)
        authority = ActiveTaskContract.load(context.core_path / "AKTIF_GOREV.md")
        bootstrap_plan = plan_fresh_bootstrap(
            home=resolved_home,
            core_root=context.core_path,
            authority_digest=authority.source_digest,
            schema=_OPERATIONAL_SCHEMA,
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
                f"persistence=sqlite; action={bootstrap_plan.action}",
            )
            table.add_row("state/operational.db", "runtime", "schema-v1")
            table.add_row("bootstrap receipt", "artifact", bootstrap_plan.plan_digest)
            console.print(table)
            raise typer.Exit(0)
        receipt = apply_fresh_bootstrap(bootstrap_plan, schema=_OPERATIONAL_SCHEMA)
        LocalCoreServices.from_context(build_context(home=context.home)).bootstrap_extensions()
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print(f"[green]Hazir:[/green] {context.home} receipt={receipt['receipt_digest']}")


@app.command()
def setup(
    apply: Annotated[
        bool,
        typer.Option(
            "--uygula",
            help="Digest-bound SQLite/local-core kurulum adimlarini gercekten uygular",
        ),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Sanitize plan JSON yazar")] = False,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
    plan_digest: Annotated[
        str | None,
        typer.Option("--plan-digest", help="Uygulanacak exact setup plan digest'i"),
    ] = None,
) -> None:
    """Yeni makine kurulumunu planlar; varsayilan davranis salt okunur dry-run'dir."""

    plan = build_setup_plan(home=resolve_home(home))
    exact_plan_digest = setup_plan_digest(plan)
    if output_json and not apply:
        plan_document = setup_plan_payload(plan)
        console.print_json(
            json.dumps(
                {
                    **plan_document,
                    "apply": apply,
                    "plan_digest": exact_plan_digest,
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
        console.print(f"Plan digest: {exact_plan_digest}")
        console.print(
            "[yellow]Dry-run. Uygulamak icin --uygula ve exact --plan-digest verin.[/yellow]"
        )
        return

    if plan_digest != exact_plan_digest:
        error_console.print("[red]Hata:[/red] setup exact --plan-digest ister")
        raise typer.Exit(EXIT_USAGE_ERROR)

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
                            "plan_digest": exact_plan_digest,
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
                    "plan_digest": exact_plan_digest,
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


def _render_doctor_repair_plan(document: dict[str, object]) -> None:
    table = Table(title="Doctor repair plani (yetki degildir)")
    table.add_column("Alan")
    table.add_column("Deger")
    table.add_row("Plan digest", str(document["plan_digest"]))
    table.add_row("Siradaki adim", str(document["action"] or "yok"))
    blocked = document["blocked_reasons"]
    assert isinstance(blocked, list)
    table.add_row("Bloke", ", ".join(str(item) for item in blocked) or "hayir")
    table.add_row(
        "Uygulanabilir",
        "evet" if document["action"] is not None and not document["blocked_reasons"] else "hayir",
    )
    console.print()
    console.print(table)
    if document["action"] is not None:
        console.print(
            "Uygulamak icin exact plan digest ile tekrar calistirin: "
            f"`{PRODUCT.cli} doctor --uygula --plan-digest {document['plan_digest']}`"
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
