"""Truthful, provider-free autonomous evolution control surface."""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, cast

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.evolution_bootstrap import (
    apply_evolution_bootstrap,
    build_evolution_bootstrap_plan,
)
from zekam.application.evolution_runtime import (
    CurrentEvolutionResumeVerifier,
    build_evolution_plan,
    build_evolution_report,
    build_resume_admission,
    build_windows_supervisor_plan,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed, ZekamError
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.sqlite.local_improvement import SQLiteLocalImprovementStore
from zekam.infrastructure.windows_task_scheduler import (
    WindowsTaskPlan,
    inspect_windows_task,
    install_windows_task,
    uninstall_windows_task,
)
from zekam.interfaces.cli import local_runtime as local_runtime_cli

app = typer.Typer(name="evolve", help="Olculu otonom iyilestirme plani ve durumu")
console = Console()
error_console = Console(stderr=True)


def _document(home: str | None) -> dict[str, object]:
    try:
        return build_evolution_plan(build_context(home=home))
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc


@app.command("plan")
def plan_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Provider cagrisi veya mutation olmadan exact OE plani uretir."""

    document = _document(home)
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"{document['state']} plan={document['plan_digest']}")


@app.command("status")
def status_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Kanonik kanittan gercek evolution hazirlik durumunu okur."""

    plan = _document(home)
    report = build_evolution_report(build_context(home=home), plan=plan)
    supervisor = cast(dict[str, object], plan["supervisor"])
    control = report["control"]
    document = {
        "schema": "zekam-evolution-status/v1",
        "state": report["state"],
        "scope_transition": plan["scope_transition"],
        "runtime": plan["runtime"],
        "evolution_authority": plan["evolution_authority"],
        "admission_bindings": plan["admission_bindings"],
        "handlers": plan["handlers"],
        "permissions": plan["permissions"],
        "capture": plan["capture"],
        "packages": plan["packages"],
        "native_acceptance": plan["native_acceptance"],
        "setup_gaps": plan["setup_gaps"],
        "blockers": plan["blockers"],
        "control": control,
        "supervisor": supervisor,
        "plan_digest": plan["plan_digest"],
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(str(document["state"]))


@app.command("candidates")
def candidates_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """List bounded candidate evidence from the canonical improvement ledger."""

    context = build_context(home=home)
    plan = build_evolution_plan(context)
    report = build_evolution_report(context, plan=plan)
    document = {
        "schema": "zekam-evolution-candidates/v1",
        "state": report["state"],
        "counts": report["counts"],
        "candidates": report["candidates"],
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"{len(document['candidates'])} candidate")


@app.command("report")
def report_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Report measured evolution evidence; never infer success from prose."""

    document = build_evolution_report(build_context(home=home))
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"{document['state']} report={document['report_digest']}")


def _control_document(
    home: str | None, operation: str
) -> tuple[SQLiteLocalImprovementStore, dict[str, object]]:
    context = build_context(home=home)
    store = LocalCoreServices.from_context(context).improvement
    current = store.evolution_control_status()
    target = {"pause": "paused", "resume": "observing", "disable": "disabled"}[operation]
    body: dict[str, object] = {
        "schema": "zekam-evolution-control-plan/v1",
        "operation": operation,
        "current_state": current["state"],
        "target_state": target,
        "provider_calls": 0,
        "network_calls": 0,
        "apply": False,
        "grants_authority": False,
    }
    if operation == "resume":
        plan = build_evolution_plan(context)
        evidence = build_resume_admission(context)
        if evidence is not None:
            body["admission_evidence"] = evidence
        body["admission_ready"] = evidence is not None
        body["admission_gaps"] = tuple(plan["setup_gaps"])
    return store, body | {"plan_digest": digest(body)}


def _control_command(home: str | None, operation: str, apply: bool) -> None:
    try:
        store, document = _control_document(home, operation)
        if apply:
            if operation == "resume" and document.get("admission_ready") is not True:
                raise ValidationFailed(
                    "Evolution resume grant/drift/recovery admission hazir degil"
                )
            target = str(document["target_state"])
            result = store.set_evolution_control_state(
                target,
                reason=f"owner-{operation}",
                now=dt.datetime.now(dt.UTC),
                admission_evidence=cast(
                    dict[str, object] | None,
                    document.get("admission_evidence"),
                ),
                admission_verifier=(
                    CurrentEvolutionResumeVerifier(build_context(home=home))
                    if operation == "resume"
                    else None
                ),
            )
            document = {
                "schema": "zekam-evolution-control-receipt/v1",
                "operation": operation,
                "plan_digest": document["plan_digest"],
                "result": result,
                "apply": True,
                "provider_calls": 0,
                "network_calls": 0,
                "grants_authority": False,
            }
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(document))


@app.command("pause")
def pause_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Stop new autonomous tick admission at a safe checkpoint."""

    _control_command(home, "pause", apply)


@app.command("resume")
def resume_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Resume only a paused local admission state; never re-enable disabled state."""

    _control_command(home, "resume", apply)


@app.command("disable")
def disable_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Permanently stop managed tick effects until exact enable is authorized."""

    _control_command(home, "disable", apply)


@app.command("run-once")
def run_once_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Run the same bounded supervisor tick, subject to the same control gate."""

    if not apply:
        console.print_json(json.dumps({
            "schema": "zekam-evolution-run-once-plan/v1",
            "apply": False,
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }))
        return
    local_runtime_cli.tick_command(owner_id="zekam-evolve-run-once", home=home)


def _windows_plan(home: str | None) -> WindowsTaskPlan:
    context = build_context(home=home)
    return build_windows_supervisor_plan(context)


@app.command("supervisor-plan")
def supervisor_plan_command(
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Show the exact Windows Task Scheduler plan without installing it."""

    try:
        document = _windows_plan(home).as_dict()
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(document))


@app.command("bootstrap-plan")
def bootstrap_plan_command(
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Show one exact v5, local standing-grant and supervisor bootstrap plan."""

    try:
        document = build_evolution_bootstrap_plan(build_context(home=home)).as_dict()
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(document))


@app.command("supervisor-status")
def supervisor_status_command(
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Read back the exact Windows task; never creates or updates it."""

    try:
        document = inspect_windows_task(_windows_plan(home))
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(document))


@app.command("enable")
def enable_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Apply the exact reviewed v5, local grant and Windows supervisor bootstrap."""

    try:
        context = build_context(home=home)
        if not apply:
            plan = build_evolution_bootstrap_plan(context)
            document = plan.as_dict() | {
                "operation": "enable",
                "standing_grant_activated": False,
            }
        elif plan_digest is None:
            raise ValidationFailed("Evolution enable --plan-digest ister")
        else:
            document = apply_evolution_bootstrap(
                context,
                authorized_plan_digest=plan_digest,
            )
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(document))


@app.command("supervisor-install")
def supervisor_install_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Install only a separately authorized exact Windows supervisor plan."""

    try:
        plan = _windows_plan(home)
        if not apply:
            document = plan.as_dict()
        elif plan_digest is None:
            raise ValidationFailed("Supervisor install --plan-digest ister")
        else:
            document = install_windows_task(
                plan,
                authorized_plan_digest=plan_digest,
            )
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(document))


@app.command("supervisor-uninstall")
def supervisor_uninstall_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Remove only the digest-authorized exact Zekam task."""

    try:
        plan = _windows_plan(home)
        if not apply:
            document = plan.as_dict() | {"operation": "uninstall"}
        elif plan_digest is None:
            raise ValidationFailed("Supervisor uninstall --plan-digest ister")
        else:
            document = uninstall_windows_task(
                plan,
                authorized_plan_digest=plan_digest,
            )
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(70) from exc
    console.print_json(json.dumps(document))
