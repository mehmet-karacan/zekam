"""`zekam worker` komutlari.

Worker sohbet surecinden bagimsiz calisir. `run` uzun omurludur ve zarif
kapanmayi destekler; `tick` tek dongu calistirip cikar (servis saglikli mi
kontrolu ve test icin).
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.chaos_command_composition import compose_command_chaos_handler
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.client_runtime_bootstrap import ClientRuntimeBootstrapService
from zekam.application.composition import build_context
from zekam.application.diagnostic_trace_composition import (
    compose_diagnostic_trace_purge_handler,
)
from zekam.application.doctor_repair import observe_git_repository
from zekam.application.home import resolve_home
from zekam.application.lifecycle_runtime_template_prepare import (
    LifecycleRuntimeTemplatePrepareService,
    run_lifecycle_template_prepare_once,
)
from zekam.application.lifecycle_template_recovery import LifecycleTemplateRecoveryService
from zekam.application.memory_compiler_composition import (
    compose_memory_candidate_compile_handler,
)
from zekam.application.realm_context import RealmContext
from zekam.application.recovery_reconciliation import (
    FailedReceiptReconciliationService,
    RecoveryReconciliationPlan,
    RecoveryReconciliationService,
)
from zekam.application.run_reconciliation import TerminalRunReconciliationService
from zekam.application.worker import (
    ScheduledHandler,
    ShutdownSignal,
    WorkerSettings,
    build_worker,
    default_capabilities,
    run_codex_runtime_once,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed, ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail_from

app = typer.Typer(name="worker", help="Worker sureci", no_args_is_help=True)
console = Console()


@app.command("lifecycle-template-prepare")
def lifecycle_template_prepare_command(
    work_id: Annotated[UUID, typer.Option("--work-id", help="Exact current Work UUID")],
    project_id: Annotated[UUID, typer.Option("--project-id", help="Exact project UUID")],
    actor_id: Annotated[UUID, typer.Option("--actor-id", help="Exact aktif human actor UUID")],
    source_revision: Annotated[
        str, typer.Option("--source-revision", help="Exact canonical source revision")
    ],
    plan_digest: Annotated[
        str | None, typer.Option("--plan-digest", help="Dry-run exact plan digest")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--uygula", help="Provider-free template prerequisites uygular")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Codex lifecycle template'ini provider cagrisi olmadan hazirlar."""

    try:
        with RealmSession(home, realm) as realm_context:
            service = LifecycleRuntimeTemplatePrepareService(
                realm_context.connection, realm_context.realm
            )
            plan = service.prepare(
                project_id=project_id,
                work_item_id=work_id,
                actor_id=actor_id,
                source_revision=source_revision,
            )
            document = plan.as_dict()
            if apply:
                if plan_digest is None:
                    raise ValidationFailed("Lifecycle template prepare --plan-digest ister")
                document = service.apply(plan, supplied_plan_digest=plan_digest)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("lifecycle-template-tick")
def lifecycle_template_tick_command(
    label: Annotated[
        str, typer.Option("--etiket", help="Dedicated lifecycle template worker etiketi")
    ] = "lifecycle-template-worker",
    apply: Annotated[bool, typer.Option("--uygula", help="Exact governed job'i isler")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Bir provider-free lifecycle template job'ini claim/receipt ile tamamlar."""

    try:
        with RealmSession(home, realm) as realm_context:
            if not apply:
                document: dict[str, object] = {
                    "schema": "zekam-lifecycle-template-tick-plan/v1",
                    "apply": False,
                    "provider_calls": 0,
                    "network_calls": 0,
                }
            else:
                result = run_lifecycle_template_prepare_once(
                    realm_context.connection,
                    realm_context.realm,
                    worker_label=label,
                )
                document = {
                    "schema": "zekam-lifecycle-template-tick-result/v1",
                    "apply": True,
                    "result": result,
                    "provider_calls": 0,
                    "network_calls": 0,
                }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("lifecycle-template-recovery")
def lifecycle_template_recovery_command(
    job_id: Annotated[UUID, typer.Option("--job-id", help="Receiptless eski prep job UUID")],
    actor_id: Annotated[UUID, typer.Option("--actor-id", help="Exact aktif human actor UUID")],
    authorization_id: Annotated[
        UUID | None, typer.Option("--authorization-id", help="Exact recovery authorization UUID")
    ] = None,
    authorize: Annotated[
        bool, typer.Option("--yetkilendir", help="Exact dry plan icin one-shot yetki uret")
    ] = False,
    apply: Annotated[bool, typer.Option("--uygula", help="Kanonik continuation uygular")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Materialize edilmis fakat receipt'siz lifecycle template job'ini uzlastirir."""

    try:
        with RealmSession(home, realm) as realm_context:
            service = LifecycleTemplateRecoveryService(
                realm_context.connection, realm_context.realm
            )
            plan = service.prepare(job_id=job_id, actor_id=actor_id)
            document: dict[str, object] = dict(plan.as_dict())
            if authorize:
                authorization = service.issue_authorization(plan, actor_id=actor_id)
                document = document | {"authorization_id": str(authorization.id)}
            if apply:
                if authorization_id is None:
                    raise ValidationFailed(
                        "Lifecycle template recovery --authorization-id ister"
                    )
                document = service.apply(
                    plan, authorization_id=authorization_id
                ).as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


def _bootstrap_source_revision(home: str | None) -> str:
    state = observe_git_repository(build_context(home=home).core_path)
    return f"git:{state.head};state:{digest(state.body())}"


@app.command("client-runtime-bootstrap")
def client_runtime_bootstrap_command(
    work_id: Annotated[UUID, typer.Option("--work-id", help="Exact proposed Work UUID")],
    project_id: Annotated[UUID, typer.Option("--project-id", help="Exact project UUID")],
    actor_id: Annotated[UUID, typer.Option("--actor-id", help="Exact aktif human actor UUID")],
    plan_digest: Annotated[
        str | None, typer.Option("--plan-digest", help="Dry-run'da uretilen exact digest")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--uygula", help="Control-plane bootstrap'i uygular")
    ] = False,
    rebootstrap: Annotated[
        bool,
        typer.Option(
            "--rebootstrap",
            help="Terminal onceki bootstrap sonrasi ayni Work icin explicit yeni revision kurar",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Pending Codex lifecycle olayi icin effect-free governed runtime kurar."""

    try:
        spool = ClientLifecycleSpool(resolve_home(home), client_id="codex")
        pending = spool.pending(limit=1)
        if not pending:
            raise ValidationFailed("Pending Codex lifecycle spool head bulunamadi")
        entry = pending[0]
        source_revision = _bootstrap_source_revision(home)
        with RealmSession(home, realm) as realm_context:
            service = ClientRuntimeBootstrapService(realm_context.connection, realm_context.realm)
            plan = service.prepare(
                project_id=project_id,
                work_item_id=work_id,
                actor_id=actor_id,
                client_id="codex",
                session_id=entry.session_id,
                entry_digest=entry.entry_digest,
                source_revision=source_revision,
                rebootstrap=rebootstrap,
            )
            document = plan.as_dict()
            if apply:
                if plan_digest is None:
                    raise ValidationFailed("Client runtime bootstrap --plan-digest ister")
                current = spool.pending(limit=1)
                if not current:
                    raise ValidationFailed("Client runtime bootstrap spool head kayboldu")
                result = service.apply(
                    plan,
                    supplied_plan_digest=plan_digest,
                    current_entry_digest=current[0].entry_digest,
                    current_source_revision=_bootstrap_source_revision(home),
                )
                document = result.as_dict() | {"bootstrap_plan_digest": plan.plan_digest}
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))


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


@app.command("reconcile-terminal-run")
def reconcile_terminal_run_command(
    run_id: Annotated[UUID, typer.Option("--run-id", help="Exact stale active run UUID")],
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
    """Yalniz terminal failed job'lara bagli stale active run'i kapatir."""
    try:
        with RealmSession(home, realm) as realm_context:
            service = TerminalRunReconciliationService(
                realm_context.connection, realm_context.realm
            )
            plan = service.prepare(run_id=run_id)
            document = plan.as_dict() | {"applied": False}
            if apply:
                if actor_id is None:
                    raise ValidationFailed("Run reconciliation --actor-id ister")
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


def _scheduled_handlers(context: RealmContext, home: str | None) -> dict[str, ScheduledHandler]:
    handlers: dict[str, ScheduledHandler] = {
        "memory-candidate-compile": compose_memory_candidate_compile_handler(
            connection=context.connection,
            realm_id=context.realm_id,
        )
    }
    handler = compose_diagnostic_trace_purge_handler(
        connection=context.connection,
        realm_id=context.realm_id,
        home=resolve_home(home),
    )
    if handler is not None:
        handlers["diagnostic-trace-purge"] = handler
    chaos_config = os.environ.get("ZEKAM_CHAOS_DRIVER_CONFIG")
    if chaos_config:
        handlers["chaos-campaign"] = compose_command_chaos_handler(Path(chaos_config))
    return handlers


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
    """Tek scheduled-only dongu. `--uygula` olmadan salt okunur."""
    try:
        with RealmSession(home, realm) as realm_context:
            worker = build_worker(
                realm_context.connection,
                realm_context.realm_id,
                settings=_settings(label, 1, 2.0),
                handlers={},
                scheduled_handlers=_scheduled_handlers(realm_context, home),
                consume_queue=False,
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


@app.command("codex-lifecycle-tick")
def codex_lifecycle_tick_command(
    label: Annotated[
        str, typer.Option("--etiket", help="Dedicated Codex lifecycle worker etiketi")
    ] = "codex-lifecycle-worker",
    apply: Annotated[bool, typer.Option("--uygula", help="Exact queue isini claim eder")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """One dedicated exact Codex lifecycle recovery/queue tick."""

    if not apply:
        document = {
            "schema": "zekam-codex-lifecycle-worker-plan/v1",
            "mode": "committed-ack-recovery-then-exact-queue-claim",
            "required_capability": "client.lifecycle.codex-drain",
            "public_authorization_mint": False,
            "applied": False,
            "grants_authority": False,
        }
    else:
        try:
            with RealmSession(home, realm) as realm_context:
                result_digest = run_codex_runtime_once(
                    realm_context.connection,
                    realm_context.realm_id,
                    home=resolve_home(home),
                    worker_label=label,
                )
            document = {
                "schema": "zekam-codex-lifecycle-worker-result/v1",
                "accepted_work": result_digest is not None,
                "result_digest": result_digest,
                "applied": result_digest is not None,
                "grants_authority": False,
            }
        except ZekamError as exc:
            raise fail_from(exc) from exc
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print_json(json.dumps(document, ensure_ascii=False))


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
    """Scheduled-only worker dongusunu baslatir; queue isi claim etmez."""
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
                handlers={},
                scheduled_handlers=_scheduled_handlers(realm_context, home),
                consume_queue=False,
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
