"""Root projection-aware close commands."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.mutation_admission import (
    DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY,
    CliMutationEvidence,
    CliMutationInvocationSnapshot,
    CliMutationTargetHints,
)
from zekam.application.projection_closure import (
    ProjectionAwareClosureService,
    ProjectionClosurePlan,
)
from zekam.application.realm_context import RealmContext
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.session_continuity import SessionCloseReceipt
from zekam.infrastructure.postgres.projection_closure_repository import (
    ProjectionClosureRepository,
)
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.interfaces.cli.memory import _session_close_receipt_from_payload
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail, fail_from

app = typer.Typer(name="close", help="Projection-aware atomic Work closure", no_args_is_help=True)
console = Console()
_MAX_CLOSE_INPUT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SessionCloseInputSnapshot:
    """One bounded read reused by admission, replay, prepare and apply."""

    input_sha256: str
    receipt: SessionCloseReceipt
    invocation: CliMutationInvocationSnapshot


def _close_input_snapshot(
    input_file: Path,
    *,
    command_path: tuple[str, ...],
    parameters: dict[str, object],
) -> SessionCloseInputSnapshot:
    if input_file.is_symlink() or not input_file.is_file():
        raise ValidationFailed("Close input regular file olmali")
    try:
        with input_file.open("rb") as stream:
            payload = stream.read(_MAX_CLOSE_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValidationFailed("Close input receipt okunamadi") from exc
    if len(payload) > _MAX_CLOSE_INPUT_BYTES:
        raise ValidationFailed("Close input receipt bounded boyutu asti")
    receipt = _session_close_receipt_from_payload(payload)
    input_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()
    evidence = CliMutationEvidence(
        kind="session-close-input",
        evidence_digest=input_sha256,
        target_hints=CliMutationTargetHints(
            project_ref=str(receipt.project_id),
            work_ref=str(receipt.work_item_id),
            run_ref=str(receipt.run_id),
            session_ref=receipt.session_id,
            client_ref=receipt.client_id,
        ),
    )
    invocation = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.snapshot(
        command_path,
        parameters,
        evidence=evidence,
    )
    return SessionCloseInputSnapshot(input_sha256, receipt, invocation)


def _service(context: RealmContext) -> ProjectionAwareClosureService:
    connection = context.connection
    realm_id = context.realm_id
    return ProjectionAwareClosureService(
        ProjectionClosureRepository(connection, realm_id),
        AuthorizationRepository(connection, realm_id),
    )


def _emit(document: dict[str, object]) -> None:
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


def _plan(
    *,
    snapshot: SessionCloseInputSnapshot,
    idempotency_key: str,
    realm: str,
    home: str | None,
) -> ProjectionClosurePlan:
    with RealmSession(home, realm, invocation=snapshot.invocation) as context:
        return _service(context).prepare(snapshot.receipt, idempotency_key=idempotency_key)


@app.command("plan")
def plan_command(
    input_file: Annotated[Path, typer.Option("--girdi", exists=True, dir_okay=False)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read current canonical state and print an authority-free exact plan."""
    try:
        snapshot = _close_input_snapshot(
            input_file,
            command_path=("close", "plan"),
            parameters={"apply": False},
        )
        plan = _plan(
            snapshot=snapshot,
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
            "input_sha256": snapshot.input_sha256,
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
        snapshot = _close_input_snapshot(
            input_file,
            command_path=("close", "apply"),
            parameters={
                "apply": True,
                "authorization_id": authorization_id,
                "claim_id": claim_id,
            },
        )
        receipt = snapshot.receipt
        with RealmSession(home, realm, invocation=snapshot.invocation) as context:
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
