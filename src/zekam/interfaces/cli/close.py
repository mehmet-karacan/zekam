"""Root projection-aware close commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.projection_closure import ProjectionAwareClosureService
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.postgres.projection_closure_repository import (
    ProjectionClosureRepository,
)
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.interfaces.cli.memory import _session_close_receipt
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail, fail_from

app = typer.Typer(name="close", help="Projection-aware atomic Work closure", no_args_is_help=True)
console = Console()


def _service(context: object) -> ProjectionAwareClosureService:
    connection = getattr(context, "connection")
    realm_id = getattr(context, "realm_id")
    return ProjectionAwareClosureService(
        ProjectionClosureRepository(connection, realm_id),
        AuthorizationRepository(connection, realm_id),
    )


def _emit(document: dict[str, object]) -> None:
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


def _plan(
    *,
    input_file: Path,
    idempotency_key: str,
    realm: str,
    home: str | None,
):
    receipt = _session_close_receipt(input_file)
    with RealmSession(home, realm) as context:
        return _service(context).prepare(receipt, idempotency_key=idempotency_key)


@app.command("plan")
def plan_command(
    input_file: Annotated[Path, typer.Option("--girdi", exists=True, dir_okay=False)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read current canonical state and print an authority-free exact plan."""
    try:
        plan = _plan(
            input_file=input_file,
            idempotency_key=idempotency_key,
            realm=realm,
            home=home,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _emit(
        plan.body()
        | {
            "plan_digest": plan.plan_digest,
            "apply": False,
            "authorization": {
                "allowed_resources": [plan.resource],
                "allowed_effects": ["database-write"],
                "effect_digest": plan.effect_digest,
                "one_shot": True,
            },
        }
    )


@app.command("apply")
def apply_command(
    input_file: Annotated[Path, typer.Option("--girdi", exists=True, dir_okay=False)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    plan_digest: Annotated[str, typer.Option("--plan-digest")],
    authorization_id: Annotated[UUID, typer.Option("--authorization-id")],
    claim_id: Annotated[UUID, typer.Option("--claim-id")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Apply only the exact planned/authenticated/claimed atomic closure."""
    if not apply:
        raise fail("Close apply mutation icin --uygula gerekir", 64)
    try:
        receipt = _session_close_receipt(input_file)
        with RealmSession(home, realm) as context:
            service = _service(context)
            replay = service.replay_completed(
                receipt,
                idempotency_key=idempotency_key,
                plan_digest=plan_digest,
                authorization_id=authorization_id,
                claim_id=claim_id,
            )
            if replay is not None:
                _emit(replay.as_dict())
                return
            plan = service.prepare(receipt, idempotency_key=idempotency_key)
            if plan.plan_digest != plan_digest:
                raise PolicyViolation("Projection closure plan digest stale veya exact degil")
            result = service.apply(
                plan,
                authorization_id=authorization_id,
                claim_id=claim_id,
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _emit(result.as_dict())
