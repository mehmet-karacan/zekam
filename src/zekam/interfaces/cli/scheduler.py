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


def _causal_chain_rows(connection: Any, work_ref: str) -> tuple[str, list[dict[str, Any]], bool]:
    with connection.cursor() as cursor:
        cursor.execute(
            "select id::text from work.work_item where id::text=%s or external_number=%s "
            "order by id limit 2",
            (work_ref, work_ref),
        )
        matches = cursor.fetchall()
        if len(matches) != 1:
            reason = "bulunamadi" if not matches else "belirsiz"
            raise ZekamError(f"work referansi {reason}: {work_ref}")
        work_id = str(matches[0][0])
        cursor.execute(
            "select record_type,node_id,source_node_id,target_node_id,kind,state,"
            "occurred_at,canonical_ref,truncated from ops.causal_chain(%s::uuid,256)",
            (work_id,),
        )
        query_rows = cursor.fetchall()
        rows = [
            {
                "record_type": str(row[0]),
                "node_id": None if row[1] is None else str(row[1]),
                "source_node_id": None if row[2] is None else str(row[2]),
                "target_node_id": None if row[3] is None else str(row[3]),
                "kind": str(row[4]),
                "state": None if row[5] is None else str(row[5]),
                "occurred_at": None if row[6] is None else row[6].isoformat(),
                "canonical_ref": None if row[7] is None else str(row[7]),
            }
            for row in query_rows
        ]
        truncated = any(bool(row[8]) for row in query_rows)
    return work_id, rows, truncated


def _orphan_rows(connection: Any) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "select orphan_kind,severity,node_id,canonical_ref,work_item_id,job_id,"
            "observed_at,reason from ops.causal_orphan "
            "order by case severity when 'critical' then 0 when 'high' then 1 else 2 end,"
            "observed_at limit 256"
        )
        return [
            {
                "orphan_kind": str(row[0]),
                "severity": str(row[1]),
                "node_id": str(row[2]),
                "canonical_ref": str(row[3]),
                "work_item_id": None if row[4] is None else str(row[4]),
                "job_id": None if row[5] is None else str(row[5]),
                "observed_at": row[6].isoformat(),
                "reason": str(row[7]),
            }
            for row in cursor.fetchall()
        ]


@report_app.command("causal-chain")
def causal_chain_command(
    work: Annotated[str, typer.Option("--work", help="Work UUID veya external number")],
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Bir isin kanonik, salt okunur nedensellik zincirini gosterir."""

    try:
        with RealmSession(home, realm) as realm_context:
            work_id, rows, truncated = _causal_chain_rows(realm_context.connection, work)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    document = {
        "schema": "zekam-causal-chain-report/v1",
        "work_item_id": work_id,
        "records": rows,
        "truncated": truncated,
        "read_only": True,
        "grants_authority": False,
    }
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
        return
    table = Table(title=f"Causal chain · {work_id}")
    table.add_column("Tur")
    table.add_column("Kaynak / Dugum")
    table.add_column("Bag / Durum")
    table.add_column("Hedef / Zaman")
    for row in rows:
        table.add_row(
            row["record_type"],
            row["node_id"] or row["source_node_id"] or "-",
            row["kind"] if row["state"] is None else f"{row['kind']} · {row['state']}",
            row["target_node_id"] or row["occurred_at"] or "-",
        )
    console.print(table)


@report_app.command("orphaned-state")
def orphaned_state_command(
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Gecikme esigini asmis yapisal kanit bosluklarini gosterir."""

    try:
        with RealmSession(home, realm) as realm_context:
            rows = _orphan_rows(realm_context.connection)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    document = {
        "schema": "zekam-orphaned-state-report/v1",
        "orphans": rows,
        "read_only": True,
        "grants_authority": False,
    }
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
        return
    table = Table(title=f"Orphaned state · {len(rows)}")
    table.add_column("Seviye")
    table.add_column("Tur")
    table.add_column("Dugum")
    table.add_column("Neden")
    for row in rows:
        table.add_row(row["severity"], row["orphan_kind"], row["node_id"], row["reason"])
    console.print(table)
