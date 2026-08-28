"""OpenCode lifecycle event bridge CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.home import resolve_home
from zekam.application.mutation_admission import (
    DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY,
    CliMutationEvidence,
    CliMutationTargetHints,
    assert_local_effect_admission,
)
from zekam.application.opencode_lifecycle import (
    OpenCodeForwardBatch,
    lifecycle_client_instance_id,
    oldest_unacknowledged_events,
    record_canonical_ack,
    record_event,
    resume_projection,
)
from zekam.application.opencode_spool import (
    apply_legacy_candidate_cleanup,
    inspect_spool,
    plan_legacy_candidate_cleanup,
)
from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.postgres.client_lifecycle_repository import ClientLifecycleRepository
from zekam.interfaces.cli.session import (
    HOME_HELP,
    REALM_HELP,
    RealmSession,
    assert_cli_invocation_backend,
    fail_from,
)

app = typer.Typer(name="opencode", help="OpenCode lifecycle ve continuity koprusu")
console = Console()


@app.command("spool-status")
def spool_status_command(
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Plugin spool durumunu path ve payload siz salt okunur raporlar."""

    try:
        status = inspect_spool(resolve_home(home))
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(status.as_dict(), ensure_ascii=False, default=str))


@app.command("spool-cleanup")
def spool_cleanup_command(
    expected_plan_digest: Annotated[str | None, typer.Option("--beklenen-plan-digest")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Exact stale legacy drain adaylarini raw delete olmadan karantinaya tasir."""

    resolved_home = resolve_home(home)
    try:
        if not apply:
            document = plan_legacy_candidate_cleanup(resolved_home).as_dict()
        else:
            invocation = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.snapshot(
                ("opencode", "spool-cleanup"), {"apply": True}
            )
            assert_cli_invocation_backend(home, invocation)
            if expected_plan_digest is None:
                raise ZekamError("--uygula exact --beklenen-plan-digest ister")
            document = apply_legacy_candidate_cleanup(
                resolved_home,
                expected_plan_digest=expected_plan_digest,
            ).as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


def _ingest_and_ack(
    repository: ClientLifecycleRepository,
    resolved_home: Path,
    instance_id: str,
    event: dict[str, object],
) -> dict[str, str]:
    document = repository.ingest(event, client_instance_id=instance_id).as_dict()
    record_canonical_ack(resolved_home, document)
    return document


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
    """Sanitize lifecycle olayini atomik yerel ledgera yazar."""
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
    console.print_json(json.dumps({"event_digest": event.document()["event_digest"]}))


@app.command("resume")
def resume_command(
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Model-bagimsiz OpenCode kesinti ozetini yazar."""
    console.print_json(json.dumps(resume_projection(resolve_home(home)), ensure_ascii=False))


@app.command("pre-compact")
def pre_compact_command(
    session_id: Annotated[str, typer.Option("--session")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Client compaction oncesi canonical structural outbox ACK kapisi."""
    resolved_home = resolve_home(home)
    try:
        with RealmSession(home, realm) as context:
            repository = ClientLifecycleRepository(context.connection, context.realm_id)
            instance_id = lifecycle_client_instance_id(resolved_home)
            acknowledgement: dict[str, str] | None = None
            for pending in oldest_unacknowledged_events(resolved_home, limit=500):
                current = _ingest_and_ack(repository, resolved_home, instance_id, dict(pending))
                if (
                    pending.get("event_type") == "session.compacting"
                    and pending.get("session_id") == session_id
                ):
                    acknowledgement = current
            if acknowledgement is None:
                event = record_event(
                    resolved_home,
                    event_type="session.compacting",
                    session_id=session_id,
                )
                acknowledgement = _ingest_and_ack(
                    repository, resolved_home, instance_id, event.document()
                )
            if "compaction_outbox_id" not in acknowledgement:
                raise ZekamError("Pre-compact canonical structural outbox ACK uretilmedi")
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps({"status": "checkpoint-acknowledged", **acknowledgement}))


@app.command("forward")
def forward_command(
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 80,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yerel v2 lifecycle olaylarini canonical PostgreSQL'e idempotent iletir."""
    resolved_home = resolve_home(home)
    batch = OpenCodeForwardBatch.capture(oldest_unacknowledged_events(resolved_home, limit=limit))
    acknowledgements: list[dict[str, str]] = []
    try:
        command_invocation = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.snapshot(
            ("opencode", "forward"), {}
        )
        assert_cli_invocation_backend(home, command_invocation)
        instance_id = lifecycle_client_instance_id(resolved_home)
        for event in batch.events:
            evidence = CliMutationEvidence(
                kind="opencode-forward-event",
                evidence_digest=event.event_digest,
                target_hints=CliMutationTargetHints(session_ref=event.session_id),
                event_type=event.event_type,
                sequence=event.sequence,
                previous_digest=event.previous_digest,
                canonical_input=event.canonical_document,
            )
            invocation = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.snapshot(
                ("opencode", "forward"),
                {},
                evidence=evidence,
            )
            # One RealmSession and repository transaction per immutable event.
            # The local ACK is written only after the canonical transaction has
            # committed; an ACK crash therefore replays the existing DB receipt.
            with RealmSession(home, realm, invocation=invocation) as context:
                repository = ClientLifecycleRepository(context.connection, context.realm_id)
                document = repository.ingest(
                    event.document(), client_instance_id=instance_id
                ).as_dict()
            record_canonical_ack(resolved_home, document)
            acknowledgements.append(document)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(
        json.dumps(
            {
                "status": "acknowledged",
                "forwarded": len(acknowledgements),
                "batch_digest": batch.batch_digest,
                "canonical_ack_digests": [item["canonical_digest"] for item in acknowledgements],
            },
            ensure_ascii=False,
        )
    )
