"""Read-only measured loop, topology, graph and tournament observability CLI."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.local_runtime_boundary import (
    AuthorizationRepository,
    PostgresMeasuredLoopRepository,
)
from zekam.application.loop_control import LoopControlService, LoopControlState
from zekam.application.loop_observatory import LoopObservatory
from zekam.domain.canonical import parse_digest
from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail, fail_from

app = typer.Typer(
    name="loop",
    help="Olcumlu loop ve execution topology salt okunur gorunumu",
    no_args_is_help=True,
)
console = Console()


def _print(document: dict[str, object], as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
        return
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


def _read(
    operation: Callable[[LoopObservatory], dict[str, object]],
    *,
    home: str | None,
    realm: str,
    as_json: bool,
) -> None:
    try:
        with RealmSession(home, realm) as context:
            document = operation(LoopObservatory(context.connection, context.realm_id))
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _print(document, as_json)


def _control(
    *,
    loop_id: UUID,
    target_state: LoopControlState,
    reason_digest: str,
    authorization_id: UUID | None,
    apply: bool,
    home: str | None,
    realm: str,
    as_json: bool,
) -> None:
    if apply and authorization_id is None:
        raise fail("Loop control --uygula exact --authorization-id ister", 64)
    try:
        parse_digest(reason_digest)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    try:
        with RealmSession(home, realm) as context:
            repository = PostgresMeasuredLoopRepository(context.connection, context.realm_id)
            service = LoopControlService(
                repository,
                AuthorizationRepository(context.connection, context.realm_id),
            )
            plan = service.prepare(
                loop_id,
                target_state=target_state,
                reason_digest=reason_digest,
            )
            if not apply:
                document: dict[str, object] = plan.body() | {
                    "control_digest": plan.control_digest,
                    "apply": False,
                }
            else:
                assert authorization_id is not None
                receipt = service.apply(plan, authorization_id=authorization_id)
                document = {
                    "schema": "zekam-loop-control-cli-receipt/v1",
                    "event_id": str(receipt.event_id),
                    "loop_id": str(receipt.loop_id),
                    "source_state": receipt.source_state.value,
                    "target_state": receipt.target_state.value,
                    "plan_digest": receipt.plan_digest,
                    "control_digest": receipt.control_digest,
                    "authorization_id": str(receipt.authorization_id),
                    "reason_digest": receipt.reason_digest,
                    "created_at": receipt.created_at,
                    "receipt_digest": receipt.receipt_digest,
                    "applied": True,
                    "grants_authority": False,
                }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _print(document, as_json)


@app.command("assess")
def assess(
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kanonik topology suitability assessment kayitlarini okur."""
    _read(
        lambda service: service.assess(work_item_id, limit=limit),
        home=home,
        realm=realm,
        as_json=as_json,
    )


@app.command("plan")
def plan(
    loop_id: Annotated[UUID, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Loop objective, metric, policy, validator ve butce baglarini okur."""
    _read(lambda service: service.plan(loop_id), home=home, realm=realm, as_json=as_json)


@app.command("status")
def status(
    loop_id: Annotated[UUID, typer.Argument()],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Loop plan, attempt, progress ve terminal durumunu birlikte okur."""
    _read(
        lambda service: service.status(loop_id, limit=limit),
        home=home,
        realm=realm,
        as_json=as_json,
    )


@app.command("attempts")
def attempts(
    loop_id: Annotated[UUID, typer.Argument()],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Loop attempt timeline ve olculmus butce kullanimini okur."""
    _read(
        lambda service: service.attempts(loop_id, limit=limit),
        home=home,
        realm=realm,
        as_json=as_json,
    )


@app.command("progress")
def progress(
    loop_id: Annotated[UUID, typer.Argument()],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """External metric vector, stop reason ve kalan butce gorunumunu okur."""
    _read(
        lambda service: service.progress(loop_id, limit=limit),
        home=home,
        realm=realm,
        as_json=as_json,
    )


@app.command("topology")
def topology(
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Topology decision kimligi, pattern ve digestlerini okur."""
    assess(work_item_id, limit, as_json, realm, home)


@app.command("graph")
def graph(
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Graph critical path, overlap, efficiency ve coordination olcumlerini okur."""
    _read(
        lambda service: service.graph(work_item_id, limit=limit),
        home=home,
        realm=realm,
        as_json=as_json,
    )


@app.command("tournament")
def tournament(
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Candidate ve bagimsiz selector metadata gorunumunu okur."""
    _read(
        lambda service: service.tournament(work_item_id, limit=limit),
        home=home,
        realm=realm,
        as_json=as_json,
    )


@app.command("ablation")
def ablation(
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Paired scaffolding ablation karar ve gate metadata gorunumunu okur."""
    _read(
        lambda service: service.ablation(work_item_id, limit=limit),
        home=home,
        realm=realm,
        as_json=as_json,
    )


@app.command("control")
def control(
    loop_id: Annotated[UUID, typer.Argument()],
    target_state: Annotated[
        LoopControlState,
        typer.Option("--state", help="Exact hedef: paused, draining, cancelled veya active"),
    ],
    reason_digest: Annotated[
        str,
        typer.Option("--reason-digest", help="Reviewed gerekcenin SHA-256 digest'i"),
    ],
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Preview or apply one exact authorization-bound loop control transition."""

    _control(
        loop_id=loop_id,
        target_state=target_state,
        reason_digest=reason_digest,
        authorization_id=authorization_id,
        apply=apply,
        home=home,
        realm=realm,
        as_json=as_json,
    )
