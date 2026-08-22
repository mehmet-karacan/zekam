"""`zekam scheduler` ve `zekam report` komutlari.

Listeleme salt okunurdur. Bu yuzey zamanlanmis isi kendisi calistirmaz; tanim ve
durum gosterir, eksik zorunlu isleri raporlar.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.worker import SchedulerGateway
from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.scheduler import (
    REQUIRED_JOB_INTERVALS,
    REQUIRED_JOBS,
    REQUIRED_REPORT_SECTIONS,
    JobDefinition,
    Schedule,
    missing_required_jobs,
    plan_trigger,
)
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail_from

scheduler_app = typer.Typer(name="scheduler", help="Zamanlanmis is islemleri", no_args_is_help=True)
report_app = typer.Typer(name="report", help="Rapor islemleri", no_args_is_help=True)
console = Console()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _defined_rows(connection: Any) -> list[tuple[str, str, str, str]]:
    """Tanimli isleri kanonik kayittan okur."""

    with connection.cursor() as cursor:
        cursor.execute(
            "select job_name, interval_spec, state, coalesce(last_run_at::text, '-')"
            " from ops.job_definition order by job_name"
        )
        return [(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in cursor.fetchall()]


@scheduler_app.command("list")
def list_command(
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Tanimli isleri ve eksik zorunlu isleri gosterir."""
    rows: list[tuple[str, str, str, str]] = []
    try:
        with RealmSession(home, realm) as realm_context:
            rows = _defined_rows(realm_context.connection)
    except ZekamError as exc:
        raise fail_from(exc) from exc

    missing = missing_required_jobs(tuple(name for name, _, _, _ in rows))
    if as_json:
        payload = {
            "definitions": [
                {"job_name": n, "interval": i, "state": s, "last_run_at": last}
                for n, i, s, last in rows
            ],
            "missing_required": list(missing),
        }
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    table = Table(title="Zamanlanmis isler")
    table.add_column("Is")
    table.add_column("Aralik")
    table.add_column("Durum")
    table.add_column("Son calisma")
    for name, interval, state, last in rows:
        table.add_row(name, interval, state, last)
    console.print(table)
    if missing:
        console.print(f"[yellow]eksik zorunlu is:[/yellow] {', '.join(missing)}")


@scheduler_app.command("init")
def init_command(
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten tanimlar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Zorunlu bakim islerini varsayilan araliklariyla tanimlar (idempotent)."""
    try:
        with RealmSession(home, realm, create_realm=True) as realm_context:
            gateway = SchedulerGateway(realm_context.connection, realm_context.realm_id)
            defined = tuple(row[0] for row in _defined_rows(realm_context.connection))
            missing = missing_required_jobs(defined)
            if not apply:
                if missing:
                    console.print(f"tanimlanacak {len(missing)} is: {', '.join(missing)}")
                else:
                    console.print("butun zorunlu bakim isleri zaten tanimli")
                console.print("[yellow]Dry-run. Tanimlamak icin --uygula verin.[/yellow]")
                return
            created = gateway.ensure_required_definitions(now=_now())
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(
        f"[green]Hazir:[/green] {len(created)} yeni tanim, "
        f"{len(REQUIRED_JOBS) - len(created)} degismedi"
    )


@scheduler_app.command("plan")
def plan_command(
    job: Annotated[str, typer.Argument(help="Is adi")],
    interval: Annotated[str | None, typer.Option("--aralik", help="Zamanlama araligi")] = None,
    last_run: Annotated[
        str | None, typer.Option("--son-calisma", help="ISO 8601 son calisma zamani")
    ] = None,
    running: Annotated[bool, typer.Option("--calisiyor", help="Onceki calisma suruyor")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Bir tetiklemenin calisip calismayacagini salt okunur hesaplar."""
    spec = interval or REQUIRED_JOB_INTERVALS.get(job)
    if spec is None:
        raise fail_from(ZekamError(f"is icin varsayilan aralik yok: {job}"))
    try:
        definition = JobDefinition(job_name=job, schedule=Schedule(interval=spec))
        previous = dt.datetime.fromisoformat(last_run) if last_run else None
        plan = plan_trigger(definition, last_run_at=previous, now=_now(), running=running)
    except (ZekamError, ValueError) as exc:
        raise fail_from(ZekamError(str(exc))) from exc

    if as_json:
        console.print_json(json.dumps(plan.as_dict(), ensure_ascii=False))
        return
    color = "green" if plan.should_run else "yellow"
    console.print(
        f"[{color}]{'calistirilir' if plan.should_run else 'atlanir'}[/{color}]: {plan.reason}"
    )
    if plan.missed:
        console.print(f"kacirilan calisma: {plan.missed}")


@scheduler_app.command("required")
def required_command(
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Kanonik bakim islerini ve varsayilan araliklarini listeler."""
    payload = [
        {"job_name": name, "interval": REQUIRED_JOB_INTERVALS[name]} for name in REQUIRED_JOBS
    ]
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title="Zorunlu bakim isleri")
    table.add_column("Is")
    table.add_column("Varsayilan aralik")
    for item in payload:
        table.add_row(item["job_name"], item["interval"])
    console.print(table)


@report_app.command("sections")
def sections_command(
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Gunluk raporda bulunmasi zorunlu bolumleri gosterir."""
    if as_json:
        console.print_json(json.dumps(list(REQUIRED_REPORT_SECTIONS), ensure_ascii=False))
        return
    for name in REQUIRED_REPORT_SECTIONS:
        console.print(f"- {name}")


@report_app.command("today")
def today_command(
    scope: Annotated[str, typer.Option("--kapsam", help="genel veya proje slug'i")] = "genel",
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Bugunun raporunu kanonik kayittan okur; rapor uretmez."""
    try:
        with RealmSession(home, realm) as realm_context, realm_context.connection.cursor() as cur:
            cur.execute(
                "select sections, report_digest from ops.daily_report"
                " where scope = %s and report_date = %s",
                (scope, _now().date()),
            )
            row = cur.fetchone()
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if row is None:
        console.print(f"[yellow]bugun icin {scope} raporu uretilmemis[/yellow]")
        raise typer.Exit(4)

    sections, report_digest = row
    if as_json:
        console.print_json(
            json.dumps({"sections": sections, "report_digest": report_digest}, ensure_ascii=False)
        )
        return
    console.print(f"# Zekam gunluk rapor ({scope})\n")
    for name in REQUIRED_REPORT_SECTIONS:
        section = sections.get(name, {})
        console.print(f"## {section.get('title', name)}")
        for line in section.get("lines") or ["kayit yok"]:
            console.print(f"- {line}")
        console.print("")
