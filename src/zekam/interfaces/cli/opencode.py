"""OpenCode lifecycle event bridge CLI."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.home import resolve_home
from zekam.application.opencode_lifecycle import record_event, resume_projection
from zekam.domain.errors import ZekamError
from zekam.interfaces.cli.session import HOME_HELP, fail_from

app = typer.Typer(name="opencode", help="OpenCode lifecycle ve continuity koprusu")
console = Console()


@app.command("event")
def event_command(
    event_type: Annotated[str, typer.Option("--type")],
    session_id: Annotated[str, typer.Option("--session")],
    parent_session_id: Annotated[str | None, typer.Option("--parent")] = None,
    agent: Annotated[str | None, typer.Option("--agent")] = None,
    model_ref: Annotated[str | None, typer.Option("--model")] = None,
    tool: Annotated[str | None, typer.Option("--tool")] = None,
    resource: Annotated[str | None, typer.Option("--resource")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    error_category: Annotated[str | None, typer.Option("--error-category")] = None,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Sanitize lifecycle olayini atomik yerel ledgera yazar."""
    try:
        event = record_event(
            resolve_home(home),
            event_type=event_type,
            session_id=session_id,
            parent_session_id=parent_session_id,
            agent=agent,
            model_ref=model_ref,
            tool=tool,
            resource=resource,
            status=status,
            error_category=error_category,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps({"event_digest": event.document()["event_digest"]}))


@app.command("resume")
def resume_command(
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Model-bagimsiz OpenCode kesinti ozetini yazar."""
    console.print_json(json.dumps(resume_projection(resolve_home(home)), ensure_ascii=False))
