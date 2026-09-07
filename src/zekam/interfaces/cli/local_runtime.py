"""Production composition surface for the fresh SQLite local runtime."""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.config import PersistenceBackend
from zekam.application.learning_daily_compiler import (
    DAILY_TIMEZONE,
    LEARNING_DAILY_OPERATION,
    LearningDailyEffectExecutor,
    latest_due_learning_day,
    latest_materialized_daily_day,
    learning_daily_scheduled_for,
)
from zekam.application.local_runtime_service import LocalEffectDispatcher, LocalRuntimeService
from zekam.application.skill_runtime import (
    SKILL_EXECUTE_OPERATION,
    SKILL_VERIFY_OPERATION,
    TrustedJournalSkillExecutor,
    TrustedJournalSkillVerifier,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.local_runtime_effects import (
    MAINTENANCE_RECONCILE_OPERATION,
    LocalJournalEffectExecutor,
    LocalJournalOutboxPublisher,
    LocalMaintenanceReconcileExecutor,
)
from zekam.infrastructure.process_identity import process_incarnation_token
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

app = typer.Typer(
    name="local-runtime",
    help="SQLite local queue/outbox/recovery durumu",
    no_args_is_help=True,
)
console = Console()
error_console = Console(stderr=True)
EXIT_RUNTIME_ERROR = 70


def _store(home: str | None) -> SQLiteLocalRuntimeStore:
    context = build_context(home=home)
    if context.settings.database.backend is not PersistenceBackend.SQLITE:
        raise PolicyViolation("Local runtime yalniz fresh SQLite operational authority kullanir")
    return SQLiteLocalRuntimeStore(context.settings.database.sqlite_path(context.home))


def _service(
    home: str | None,
    *,
    effect_pause_ms: int = 0,
    outbox_pause_ms: int = 0,
) -> tuple[SQLiteLocalRuntimeStore, LocalRuntimeService, LocalCoreServices]:
    context = build_context(home=home)
    if context.settings.database.backend is not PersistenceBackend.SQLITE:
        raise PolicyViolation("Local runtime yalniz fresh SQLite operational authority kullanir")
    store = SQLiteLocalRuntimeStore(context.settings.database.sqlite_path(context.home))
    core = LocalCoreServices.from_context(context)
    effects_root = context.home / "runtime" / "local-effects"
    journal = LocalJournalEffectExecutor(
        effects_root, pause_after_write_ms=effect_pause_ms
    )
    maintenance = LocalMaintenanceReconcileExecutor(effects_root)
    learning_daily = LearningDailyEffectExecutor(
        core.learning,
        core.operational,
        KnowledgeFileStore(context.home),
    )
    skill_executor = TrustedJournalSkillExecutor(
        core.learning, effects_root, core.skill_runtime_signer
    )
    skill_verifier = TrustedJournalSkillVerifier(
        core.learning, effects_root, core.skill_runtime_signer
    )
    service = LocalRuntimeService(
        store,
        effect_dispatcher=LocalEffectDispatcher(
            (
                ("local.append-journal/v1", journal),
                (MAINTENANCE_RECONCILE_OPERATION, maintenance),
                (LEARNING_DAILY_OPERATION, learning_daily),
                (SKILL_EXECUTE_OPERATION, skill_executor),
                (SKILL_VERIFY_OPERATION, skill_verifier),
            )
        ),
        outbox_publisher=LocalJournalOutboxPublisher(
            effects_root, pause_after_write_ms=outbox_pause_ms
        ),
    )
    return store, service, core


@app.command("status")
def status_command(
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Queue/outbox/recovery sayaclarini payload gostermeden raporlar."""
    try:
        status = _store(home).status()
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print_json(json.dumps(asdict(status)))


@app.command("recovery-cases")
def recovery_cases_command(
    all_cases: Annotated[bool, typer.Option("--tumunu-goster")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Belirsiz external effect vakalarini payload gostermeden listeler."""
    try:
        cases = _store(home).recovery_cases(open_only=not all_cases)
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print_json(json.dumps([asdict(case) for case in cases]))


@app.command("recover")
def recover_command(
    apply: Annotated[bool, typer.Option("--uygula", help="Recovery sweep uygular")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Dead/expired lease ve belirsiz outbox teslimlerini fail-closed uzlastirir."""
    try:
        store = _store(home)
        if not apply:
            document: dict[str, object] = {
                "schema": "zekam-local-runtime-recovery-plan/v1",
                "apply": False,
                "status": asdict(store.status()),
                "provider_calls": 0,
                "network_calls": 0,
            }
        else:
            jobs = store.recover_orphans(process_incarnation_token)
            expired = store.recover_expired()
            outbox = store.recover_outbox(process_incarnation_token)
            document = {
                "schema": "zekam-local-runtime-recovery-result/v1",
                "apply": True,
                "orphan_jobs": asdict(jobs),
                "expired_jobs": asdict(expired),
                "recovered_outbox": outbox,
                "status": asdict(store.status()),
                "provider_calls": 0,
                "network_calls": 0,
            }
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print_json(json.dumps(document))


@app.command("submit-journal")
def submit_journal_command(
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    relative_path: Annotated[str, typer.Option("--relative-path")],
    line: Annotated[str, typer.Option("--line")],
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Runtime root altinda claim-before-effect ile islenecek yerel journal isi birakir."""
    try:
        job, created = _store(home).enqueue(
            idempotency_key=idempotency_key,
            payload={
                "operation": "local.append-journal/v1",
                "effect": {"relative_path": relative_path, "line": line},
            },
        )
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print_json(json.dumps({"job_id": job.id, "state": job.state, "created": created}))


def _identity() -> tuple[int, str]:
    pid = os.getpid()
    token = process_incarnation_token(pid)
    if token is None:
        raise PolicyViolation("Current process incarnation token okunamadi")
    return pid, token


@app.command("tick")
def tick_command(
    owner_id: Annotated[str, typer.Option("--owner-id")] = "zekam-os-supervisor",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Run one bounded, idempotent OS-supervisor maintenance tick."""

    try:
        pid, token = _identity()
        store, service, core = _service(home)
        control = core.improvement.evolution_control_status()
        if control["state"] in {"paused", "disabled"}:
            console.print_json(
                json.dumps(
                    {
                        "schema": "zekam-os-supervisor-tick-receipt/v1",
                        "state": control["state"],
                        "control": control,
                        "job_created": False,
                        "daily_job_created": False,
                        "terminal_state": "not-admitted",
                        "daily_terminal_state": "not-admitted",
                        "provider_calls": 0,
                        "network_calls": 0,
                        "grants_authority": False,
                    }
                )
            )
            return
        recovered_outbox = service.startup_outbox(process_incarnation_token)
        pre_delivered = 0
        for _ in range(8):
            claim = service.publish_outbox_once(
                owner_id=f"{owner_id}-outbox",
                owner_pid=pid,
                owner_token=token,
            )
            if claim is None:
                break
            pre_delivered += 1
        startup = service.startup(process_incarnation_token)
        now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
        scheduled_for = now.replace(minute=(now.minute // 5) * 5)
        schedule_body = {
            "schema": "zekam-maintenance-reconcile-schedule/v1",
            "interval_minutes": 5,
            "scheduled_for": scheduled_for.isoformat().replace("+00:00", "Z"),
            "operation": MAINTENANCE_RECONCILE_OPERATION,
            "misfire": "run-once",
            "overlap": "skip",
        }
        schedule_digest = digest(schedule_body)
        job, created = store.schedule_once(
            slot_key=f"maintenance-reconcile:{schedule_body['scheduled_for']}",
            schedule_digest=schedule_digest,
            idempotency_key=f"maintenance-reconcile:{schedule_digest}",
            payload={
                "operation": MAINTENANCE_RECONCILE_OPERATION,
                "effect": {
                    "scheduled_for": schedule_body["scheduled_for"],
                    "schedule_digest": schedule_digest,
                    "source": "os-supervisor",
                },
            },
        )
        work = service.run_worker_once(
            owner_id=owner_id,
            owner_pid=pid,
            owner_token=token,
            job_id=job.id,
        )
        due_day = latest_due_learning_day(now)
        completed_day = store.latest_completed_learning_day()
        materialized_day = latest_materialized_daily_day(
            core.operational, KnowledgeFileStore(core.operational_path.parent.parent)
        )
        if completed_day != materialized_day:
            raise PolicyViolation("Learning daily runtime/file watermark drift")
        daily_job = None
        daily_work = None
        daily_created = False
        daily_snapshot = None
        if completed_day is None or completed_day < due_day:
            earliest_day = core.learning.earliest_learning_day(DAILY_TIMEZONE)
            start_day = (
                min(due_day, earliest_day)
                if completed_day is None and earliest_day is not None
                else due_day
                if completed_day is None
                else completed_day + dt.timedelta(days=1)
            )
            daily_scheduled_for = learning_daily_scheduled_for(due_day)
            daily_body = {
                "schema": "zekam-learning-daily-schedule/v1",
                "start_day": start_day.isoformat(),
                "day": due_day.isoformat(),
                "scheduled_for": daily_scheduled_for.isoformat().replace("+00:00", "Z"),
                "operation": LEARNING_DAILY_OPERATION,
                "misfire": "run-once",
                "timezone": DAILY_TIMEZONE,
            }
            daily_schedule_digest = digest(daily_body)
            daily_job, daily_created = store.schedule_once(
                slot_key=f"learning-daily:{due_day.isoformat()}",
                schedule_digest=daily_schedule_digest,
                idempotency_key=f"learning-daily:{daily_schedule_digest}",
                payload={
                    "operation": LEARNING_DAILY_OPERATION,
                    "effect": {
                        "start_day": daily_body["start_day"],
                        "day": daily_body["day"],
                        "scheduled_for": daily_body["scheduled_for"],
                        "schedule_digest": daily_schedule_digest,
                        "source": "os-supervisor",
                        "timezone": DAILY_TIMEZONE,
                    },
                },
            )
            daily_work = service.run_worker_once(
                owner_id=owner_id,
                owner_pid=pid,
                owner_token=token,
                job_id=daily_job.id,
            )
            daily_snapshot = store.job_snapshot(daily_job.id)
        post_delivered = 0
        for _ in range(8):
            claim = service.publish_outbox_once(
                owner_id=f"{owner_id}-outbox",
                owner_pid=pid,
                owner_token=token,
            )
            if claim is None:
                break
            post_delivered += 1
        snapshot = store.job_snapshot(job.id)
        if (
            snapshot is None
            or snapshot.get("state") != "completed"
            or not isinstance(snapshot.get("terminal_evidence_digest"), str)
        ):
            raise PolicyViolation("Supervisor tick exact scheduled job terminal receipt ister")
        if daily_job is not None and (
            daily_snapshot is None
            or daily_snapshot.get("state") != "completed"
            or not isinstance(daily_snapshot.get("terminal_evidence_digest"), str)
        ):
            raise PolicyViolation("Supervisor tick daily job terminal receipt ister")
        document = {
            "schema": "zekam-os-supervisor-tick-receipt/v1",
            "scheduled_for": schedule_body["scheduled_for"],
            "schedule_digest": schedule_digest,
            "job_id": job.id,
            "job_created": created,
            "claimed_job_id": None if work is None else work.job.id,
            "terminal_state": snapshot["state"],
            "terminal_evidence_digest": snapshot["terminal_evidence_digest"],
            "daily_due_day": due_day.isoformat(),
            "daily_previous_completed_day": (
                None if completed_day is None else completed_day.isoformat()
            ),
            "daily_job_id": None if daily_job is None else daily_job.id,
            "daily_job_created": daily_created,
            "daily_claimed_job_id": (
                None if daily_work is None else daily_work.job.id
            ),
            "daily_terminal_state": (
                "not-due" if daily_snapshot is None else daily_snapshot["state"]
            ),
            "daily_terminal_evidence_digest": (
                None
                if daily_snapshot is None
                else daily_snapshot["terminal_evidence_digest"]
            ),
            "startup": asdict(startup),
            "recovered_outbox": recovered_outbox,
            "delivered_outbox": pre_delivered + post_delivered,
            "status": asdict(store.status()),
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }
        document["receipt_digest"] = digest(document)
    except (KeyError, ZekamError) as exc:
        if isinstance(exc, KeyError):
            exc = ValidationFailed("Supervisor tick terminal readback eksik")
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print_json(json.dumps(document))


@app.command("worker-once")
def worker_once_command(
    owner_id: Annotated[str, typer.Option("--owner-id")] = "zekam-local-worker",
    home: Annotated[str | None, typer.Option("--home")] = None,
    pause_after_effect_ms: Annotated[
        int,
        typer.Option("--pause-after-effect-ms", hidden=True, min=0, max=60_000),
    ] = 0,
) -> None:
    """Startup recovery yapar ve en fazla bir queued local effect isler."""
    try:
        pid, token = _identity()
        store, service, _core = _service(home, effect_pause_ms=pause_after_effect_ms)
        startup = service.startup(process_incarnation_token)
        work = service.run_worker_once(
            owner_id=owner_id,
            owner_pid=pid,
            owner_token=token,
        )
        document = {
            "startup": asdict(startup),
            "claimed_job_id": None if work is None else work.job.id,
            "status": asdict(store.status()),
        }
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print_json(json.dumps(document))


@app.command("outbox-once")
def outbox_once_command(
    owner_id: Annotated[str, typer.Option("--owner-id")] = "zekam-local-outbox",
    home: Annotated[str | None, typer.Option("--home")] = None,
    pause_after_delivery_ms: Annotated[
        int,
        typer.Option("--pause-after-delivery-ms", hidden=True, min=0, max=60_000),
    ] = 0,
) -> None:
    """Startup recovery yapar ve en fazla bir fenced outbox eventi teslim eder."""
    try:
        pid, token = _identity()
        store, service, _core = _service(home, outbox_pause_ms=pause_after_delivery_ms)
        recovered_outbox = service.startup_outbox(process_incarnation_token)
        claim = service.publish_outbox_once(
            owner_id=owner_id,
            owner_pid=pid,
            owner_token=token,
        )
        document = {
            "startup": {"recovered_outbox": recovered_outbox},
            "claimed_outbox_id": None if claim is None else claim.event.id,
            "status": asdict(store.status()),
        }
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print_json(json.dumps(document))


@app.command("resolve")
def resolve_command(
    recovery_case_id: Annotated[str, typer.Option("--case-id")],
    outcome: Annotated[str, typer.Option("--outcome")],
    evidence: Annotated[str, typer.Option("--evidence-digest")],
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Human-verified unknown effect/delivery icin immutable resolution receipt yazar."""
    if outcome not in {"completed", "failed", "delivered"}:
        raise typer.BadParameter("outcome completed/failed/delivered olmali")
    try:
        store = _store(home)
        resolution = store.resolve_recovery(
            recovery_case_id,
            outcome=outcome,  # type: ignore[arg-type]
            evidence_digest=evidence,
        )
        cases = store.recovery_cases(open_only=False)
        matching = next(case for case in cases if case.id == recovery_case_id)
        job = None
        if matching.case_kind == "effect-unknown":
            job = store.reconcile_recovery(matching.job_id)
        document = {
            "resolution": asdict(resolution),
            "job_state": None if job is None else job.state,
            "status": asdict(store.status()),
        }
    except (StopIteration, ZekamError) as exc:
        if isinstance(exc, StopIteration):
            exc = ValidationFailed("Resolved recovery case okunamadi")
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print_json(json.dumps(document))
