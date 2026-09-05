"""Production composition surface for the fresh SQLite local runtime."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.config import PersistenceBackend
from zekam.application.local_runtime_service import LocalRuntimeService
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.infrastructure.local_runtime_effects import (
    LocalJournalEffectExecutor,
    LocalJournalOutboxPublisher,
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
) -> tuple[SQLiteLocalRuntimeStore, LocalRuntimeService]:
    context = build_context(home=home)
    if context.settings.database.backend is not PersistenceBackend.SQLITE:
        raise PolicyViolation("Local runtime yalniz fresh SQLite operational authority kullanir")
    store = SQLiteLocalRuntimeStore(context.settings.database.sqlite_path(context.home))
    effects_root = context.home / "runtime" / "local-effects"
    return store, LocalRuntimeService(
        store,
        effect_executor=LocalJournalEffectExecutor(
            effects_root, pause_after_write_ms=effect_pause_ms
        ),
        outbox_publisher=LocalJournalOutboxPublisher(
            effects_root, pause_after_write_ms=outbox_pause_ms
        ),
    )


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
        store, service = _service(home, effect_pause_ms=pause_after_effect_ms)
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
        store, service = _service(home, outbox_pause_ms=pause_after_delivery_ms)
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
