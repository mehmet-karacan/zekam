"""Windows OpenCode lifecycle bridge CLI.

The OpenCode plugin invokes this surface from a child process.  The Windows
profile keeps lifecycle evidence in the local, content-free durable ledger;
it must not depend on the removed PostgreSQL runtime.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.home import resolve_home
from zekam.application.mutation_admission import assert_local_effect_admission
from zekam.application.opencode_agent_bootstrap import (
    apply_opencode_agent_bootstrap,
    plan_opencode_agent_bootstrap,
)
from zekam.application.opencode_lifecycle import record_event, resume_projection
from zekam.application.opencode_spool import (
    apply_legacy_candidate_cleanup,
    drain_plugin_spool,
    inspect_spool,
    plan_legacy_candidate_cleanup,
)
from zekam.domain.errors import ConfigurationError, ZekamError
from zekam.interfaces.cli.session import HOME_HELP, fail_from

app = typer.Typer(name="opencode", help="OpenCode lifecycle ve continuity koprusu")
console = Console()


@app.command("install")
def install_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
) -> None:
    """Managed OpenCode agents and lifecycle plugin installation plan/apply."""

    try:
        discovered = shutil.which("opencode")
        executable = Path(discovered).resolve(strict=True) if discovered else None
        plan = plan_opencode_agent_bootstrap(executable=executable, user_home=Path.home())
        document = {
            "schema": "zekam-opencode-install/v1",
            "available": plan.available,
            "config_update_required": plan.config_update_required,
            "agents_to_create": list(plan.agents_to_create),
            "agents_to_update": list(plan.agents_to_update),
            "conflicting_agents": list(plan.conflicting_agents),
            "lifecycle_plugin_update_required": plan.lifecycle_plugin_to_create,
            "lifecycle_plugin_conflict": plan.lifecycle_plugin_conflict,
            "apply": apply,
            "grants_authority": False,
        }
        if apply:
            apply_opencode_agent_bootstrap(plan)
    except OSError as exc:
        raise fail_from(ConfigurationError("OpenCode executable cozumlenemedi")) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False))


@app.command("spool-drain")
def spool_drain_command(
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 500,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Typed plugin olaylarini bounded olarak yerel ledgera aktarir."""

    assert_local_effect_admission(("opencode", "spool-drain"))
    try:
        document = drain_plugin_spool(resolve_home(home), limit=limit).as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("spool-status")
def spool_status_command(
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Plugin spool durumunu path ve payload icermeden raporlar."""

    try:
        document = inspect_spool(resolve_home(home)).as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("spool-cleanup")
def spool_cleanup_command(
    expected_plan_digest: Annotated[str | None, typer.Option("--beklenen-plan-digest")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Stale drain adaylarini raw delete olmadan karantinaya tasir."""

    resolved_home = resolve_home(home)
    try:
        if not apply:
            document = plan_legacy_candidate_cleanup(resolved_home).as_dict()
        else:
            if expected_plan_digest is None:
                raise ZekamError("--uygula exact --beklenen-plan-digest ister")
            document = apply_legacy_candidate_cleanup(
                resolved_home,
                expected_plan_digest=expected_plan_digest,
            ).as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("event")
def event_command(
    event_type: Annotated[str, typer.Option("--type")],
    session_id: Annotated[str, typer.Option("--session")],
    delivery_id: Annotated[str | None, typer.Option("--delivery-id")] = None,
    parent_session_id: Annotated[str | None, typer.Option("--parent")] = None,
    agent: Annotated[str | None, typer.Option("--agent")] = None,
    model_ref: Annotated[str | None, typer.Option("--model")] = None,
    tool: Annotated[str | None, typer.Option("--tool")] = None,
    resource: Annotated[str | None, typer.Option("--resource")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    error_category: Annotated[str | None, typer.Option("--error-category")] = None,
    completed_summary: Annotated[str | None, typer.Option("--completed")] = None,
    pending_summary: Annotated[str | None, typer.Option("--pending")] = None,
    next_action: Annotated[str | None, typer.Option("--next-action")] = None,
    task_label: Annotated[str | None, typer.Option("--task-label")] = None,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Sanitize edilmis lifecycle olayini atomik yerel ledgera yazar."""

    assert_local_effect_admission(("opencode", "event"))
    try:
        event = record_event(
            resolve_home(home),
            event_type=event_type,
            session_id=session_id,
            delivery_id=delivery_id,
            parent_session_id=parent_session_id,
            agent=agent,
            model_ref=model_ref,
            tool=tool,
            resource=resource,
            status=status,
            error_category=error_category,
            completed_summary=completed_summary,
            pending_summary=pending_summary,
            next_action=next_action,
            task_label=task_label,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(
        json.dumps(
            {
                "schema": "zekam-opencode-event-ack/v1",
                "status": "durable-local-ack",
                "event_digest": event.document()["event_digest"],
                "contains_prompt": False,
                "contains_response": False,
                "grants_authority": False,
            }
        )
    )


@app.command("resume")
def resume_command(
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Model-bagimsiz OpenCode kesinti ozetini yazar."""

    console.print_json(json.dumps(resume_projection(resolve_home(home)), ensure_ascii=False))


@app.command("pre-compact")
def pre_compact_command(
    session_id: Annotated[str, typer.Option("--session")],
    delivery_id: Annotated[str | None, typer.Option("--delivery-id")] = None,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Compaction oncesi content-free yerel durable ACK uretir."""

    assert_local_effect_admission(("opencode", "pre-compact"))
    try:
        event = record_event(
            resolve_home(home),
            event_type="session.compacting",
            session_id=session_id,
            delivery_id=delivery_id,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(
        json.dumps(
            {
                "schema": "zekam-opencode-precompact-ack/v1",
                "status": "checkpoint-acknowledged",
                "durability": "local-ledger",
                "event_digest": event.document()["event_digest"],
                "contains_prompt": False,
                "contains_response": False,
                "grants_authority": False,
            }
        )
    )
