"""`zekam worker` komutlari.

Worker sohbet surecinden bagimsiz calisir. `run` uzun omurludur ve zarif
kapanmayi destekler; `tick` tek dongu calistirip cikar (servis saglikli mi
kontrolu ve test icin).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.recovery_reconciliation import (
    FailedReceiptReconciliationService,
    RecoveryReconciliationPlan,
    RecoveryReconciliationService,
)
from zekam.application.worker import (
    ShutdownSignal,
    WorkerSettings,
    build_worker,
    default_capabilities,
    resolve_handlers,
)
from zekam.domain.errors import ValidationFailed, ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.runtime import JobKind
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail_from

app = typer.Typer(name="worker", help="Worker sureci", no_args_is_help=True)
console = Console()


@app.command("reconcile-failed-receipt")
def reconcile_failed_receipt_command(
    job_id: Annotated[UUID, typer.Option("--job", help="Recovery-required job UUID")],
    claim_id: Annotated[UUID, typer.Option("--claim", help="Exact effect claim UUID")],
    receipt_id: Annotated[UUID, typer.Option("--receipt", help="Exact failed receipt UUID")],
    actor_id: Annotated[
        UUID | None, typer.Option("--actor-id", help="Exact aktif human actor UUID")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--uygula", help="One-shot authorization ile uzlastirir")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Mevcut failed receipt'i orphan runtime lease/job ile kanonik kapatir."""

    try:
        with RealmSession(home, realm) as realm_context:
            service = FailedReceiptReconciliationService(
                realm_context.connection, realm_context.realm
            )
            plan = service.prepare(job_id=job_id, claim_id=claim_id, receipt_id=receipt_id)
            document = plan.as_dict() | {"applied": False}
            if apply:
                if actor_id is None:
                    raise ValidationFailed("Failed receipt reconciliation --actor-id ister")
                authorization = service.issue_authorization(plan, actor_id=actor_id)
                document = service.apply(plan, authorization_id=authorization.id) | {
                    "authorization_id": str(authorization.id),
                    "applied": True,
                }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _settings(label: str, iterations: int | None, poll: float) -> WorkerSettings:
    return WorkerSettings(
        worker_label=label,
        capabilities=default_capabilities(),
        poll_seconds=poll,
        max_iterations=iterations,
    )


def _recovery_plan(input_file: Path) -> RecoveryReconciliationPlan:
    try:
        document = json.loads(input_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Recovery girdisi okunamadi veya JSON gecersiz") from exc
    if not isinstance(document, Mapping):
        raise ValidationFailed("Recovery girdisi JSON object olmali")
    return RecoveryReconciliationPlan.from_dict(document)


@app.command("settings")
def settings_command(
    label: Annotated[str, typer.Option("--etiket", help="Worker etiketi")] = "worker-1",
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Worker sinirlarini gosterir. Salt okunur."""
    try:
        settings = _settings(label, None, 2.0)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if as_json:
        console.print_json(json.dumps(settings.as_dict(), ensure_ascii=False))
        return
    table = Table(title="Worker ayarlari")
    table.add_column("Alan")
    table.add_column("Deger")
    for key, value in settings.as_dict().items():
        table.add_row(key, ", ".join(value) if isinstance(value, list) else str(value))
    console.print(table)


@app.command("tick")
def tick_command(
    label: Annotated[str, typer.Option("--etiket", help="Worker etiketi")] = "worker-1",
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten isler")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Tek dongu: kapasite, zamanlama ve kuyruk. `--uygula` olmadan salt okunur."""
    try:
        with RealmSession(home, realm) as realm_context:
            worker = build_worker(
                realm_context.connection,
                realm_context.realm_id,
                settings=_settings(label, 1, 2.0),
                handlers=(resolve_handlers([str(item) for item in JobKind]) if apply else {}),
                allow_empty_handlers=not apply,
            )
            result = worker.tick(now=_now()) if apply else worker.plan(now=_now())
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if as_json:
        console.print_json(json.dumps(result.as_dict(), ensure_ascii=False))
        return
    if result.triggered_jobs:
        console.print(f"tetiklenen is: {', '.join(result.triggered_jobs)}")
    if result.accepted_work:
        console.print(f"[green]is islendi:[/green] {result.job_id} -> {result.outcome}")
    else:
        console.print(f"[yellow]is alinmadi:[/yellow] {result.skipped_reason}")


@app.command("run")
def run_command(
    label: Annotated[str, typer.Option("--etiket", help="Worker etiketi")] = "worker-1",
    iterations: Annotated[
        int | None, typer.Option("--dongu", help="Azami dongu sayisi (bos: sinirsiz)")
    ] = None,
    poll: Annotated[float, typer.Option("--bekleme", help="Bos kuyrukta bekleme saniyesi")] = 2.0,
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten calistirir")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Worker dongusunu baslatir. SIGINT/SIGTERM ile zarifce kapanir."""
    if not apply:
        console.print("[yellow]Dry-run. Worker'i baslatmak icin --uygula verin.[/yellow]")
        console.print("Ne olacagini gormek icin: zekam worker tick --json")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            worker = build_worker(
                realm_context.connection,
                realm_context.realm_id,
                settings=_settings(label, iterations, poll),
                handlers=resolve_handlers([str(item) for item in JobKind]),
            )
            shutdown = ShutdownSignal()
            shutdown.install()
            worker.shutdown = shutdown
            console.print(f"[green]worker basladi:[/green] {label} (durdurmak icin Ctrl+C)")
            results = worker.run()
    except ZekamError as exc:
        raise fail_from(exc) from exc

    processed = sum(1 for item in results if item.accepted_work)
    triggered = sum(len(item.triggered_jobs) for item in results)
    console.print(
        f"worker durdu ({shutdown.reason or 'dongu siniri'}): "
        f"{len(results)} dongu, {processed} is, {triggered} tetikleme"
    )


@app.command("recovery-authorize")
def recovery_authorize_command(
    input_file: Annotated[
        Path,
        typer.Option("--girdi", exists=True, dir_okay=False, help="Exact recovery JSON plani"),
    ],
    actor_id: Annotated[
        UUID | None,
        typer.Option("--actor-id", help="Yetkiyi veren canonical human actor UUID"),
    ] = None,
    apply: Annotated[bool, typer.Option("--uygula", help="Exact one-shot yetkiyi uretir")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Recovery effect/plan digest'ini hazirlar; `--uygula` ile exact yetki uretir."""

    try:
        plan = _recovery_plan(input_file)
        with RealmSession(home, realm) as realm_context:
            service = RecoveryReconciliationService(realm_context.connection, realm_context.realm)
            service.validate(plan)
            document = plan.as_dict()
            if apply:
                if actor_id is None:
                    raise ValidationFailed("Recovery authorization --actor-id ister")
                authorization = service.issue_authorization(plan, actor_id=actor_id)
                document |= {
                    "dry_run": False,
                    "authorization_id": str(authorization.id),
                    "authorization_digest": authorization.authorization_digest,
                    "authorization_state": authorization.state.value,
                }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("reconcile-recovery")
def reconcile_recovery_command(
    input_file: Annotated[
        Path,
        typer.Option("--girdi", exists=True, dir_okay=False, help="Exact recovery JSON plani"),
    ],
    authorization_id: Annotated[
        UUID | None,
        typer.Option("--authorization-id", help="Pre-issued exact recovery authorization UUID"),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--uygula", help="Authorization'i tuketip recovery'yi uygular")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Receiptless eski claim'i checkpoint ve terminal receipt ile uzlastirir."""

    try:
        plan = _recovery_plan(input_file)
        with RealmSession(home, realm) as realm_context:
            service = RecoveryReconciliationService(realm_context.connection, realm_context.realm)
            service.validate(plan)
            if apply:
                if authorization_id is None:
                    raise ValidationFailed("Recovery apply --authorization-id ister")
                result = service.apply(plan, authorization_id=authorization_id)
                document = (
                    plan.as_dict() | result.as_dict() | {"authorization_id": str(authorization_id)}
                )
            else:
                document = plan.as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
