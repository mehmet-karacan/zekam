"""OpenCode lifecycle event bridge CLI."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.home import resolve_home
from zekam.application.opencode_lifecycle import (
    lifecycle_client_instance_id,
    oldest_unacknowledged_events,
    record_canonical_ack,
    record_event,
    resume_projection,
)
from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.postgres.client_lifecycle_repository import ClientLifecycleRepository
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail_from

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
    completed_summary: Annotated[str | None, typer.Option("--completed")] = None,
    pending_summary: Annotated[str | None, typer.Option("--pending")] = None,
    next_action: Annotated[str | None, typer.Option("--next-action")] = None,
    task_label: Annotated[str | None, typer.Option("--task-label")] = None,
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
            completed_summary=completed_summary,
            pending_summary=pending_summary,
            next_action=next_action,
            task_label=task_label,
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


@app.command("forward")
def forward_command(
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 80,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yerel v2 lifecycle olaylarini canonical PostgreSQL'e idempotent iletir."""
    resolved_home = resolve_home(home)
    events = list(oldest_unacknowledged_events(resolved_home, limit=limit))
    acknowledgements: list[dict[str, str]] = []
    try:
        with RealmSession(home, realm) as context:
            repository = ClientLifecycleRepository(context.connection, context.realm_id)
            instance_id = lifecycle_client_instance_id(resolved_home)
            for event in events:
                document = repository.ingest(event, client_instance_id=instance_id).as_dict()
                record_canonical_ack(resolved_home, document)
                acknowledgements.append(document)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(
        json.dumps(
            {
                "status": "acknowledged",
                "forwarded": len(acknowledgements),
                "canonical_ack_digests": [
                    item["canonical_digest"] for item in acknowledgements
                ],
            },
            ensure_ascii=False,
        )
    )
