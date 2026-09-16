"""CLI integration policy, reconciliation and rollback surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from zekam.application.client_integrations import (
    apply_rollback_plan,
    apply_sync_plan,
    build_rollback_plan,
    build_sync_plan,
    integration_mutation_resource,
    integration_status,
    rollback_receipt,
    sync_receipt,
    sync_receipt_by_digest,
)
from zekam.application.composition import build_context
from zekam.application.home import resolve_home
from zekam.application.project_rag_runtime import resolve_project_source
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.interfaces.cli.session import HOME_HELP, sqlite_operational_store
from zekam.interfaces.cli.skill import _paths, _run_claimed_effect

app = typer.Typer(
    name="integration",
    help="CLI integration tercihi, managed artifact sync ve rollback",
    no_args_is_help=True,
)


def _emit(document: object) -> None:
    typer.echo(json.dumps(document, ensure_ascii=True, sort_keys=True, default=str))


def _bound_project_root(home: str | None, requested: Path) -> Path:
    resolved_home = resolve_home(home)
    exact = requested.resolve(strict=True)
    store = sqlite_operational_store(home, DEFAULT_REALM_SLUG)
    if store is None:
        raise PolicyViolation("CLI integration project registry unavailable")
    with store.unit_of_work() as uow:
        projects = uow.list_projects()
    matches = []
    for project in projects:
        try:
            bound = resolve_project_source(resolved_home, project.slug).resolve(strict=True)
        except ZekamError:
            continue
        if bound == exact:
            matches.append(project)
    if len(matches) != 1:
        raise PolicyViolation("CLI integration exact registered project source root ister")
    return exact


def _registered_projects(home: str | None) -> tuple[tuple[str, Path], ...]:
    resolved_home = resolve_home(home)
    context = build_context(home=home)
    database = context.settings.database.sqlite_path(context.home)
    if not database.is_file() or database.is_symlink():
        return ()
    store = sqlite_operational_store(home, DEFAULT_REALM_SLUG)
    if store is None:
        return ()
    with store.unit_of_work() as uow:
        projects = uow.list_projects()
    resolved: list[tuple[str, Path]] = []
    for project in projects:
        try:
            source = resolve_project_source(resolved_home, project.slug).resolve(strict=True)
        except ZekamError:
            continue
        resolved.append((project.slug, source))
    return tuple(sorted(resolved, key=lambda item: item[0]))


@app.command("status")
def status_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    project_root: Annotated[
        Path | None, typer.Option("--project-root", exists=True, file_okay=False)
    ] = None,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Report enabled, supported, installed and cleanup state separately."""

    del output_json
    try:
        bound = None if project_root is None else _bound_project_root(home, project_root)
        _emit(integration_status(build_context(home=home), project_root=bound))
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("sync")
def sync_command(
    scope: Annotated[str, typer.Option("--scope")] = "user",
    project_root: Annotated[
        Path | None, typer.Option("--project-root", exists=True, file_okay=False)
    ] = None,
    enable: Annotated[list[str] | None, typer.Option("--enable")] = None,
    disable: Annotated[list[str] | None, typer.Option("--disable")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Build a read-only sync plan, or apply the exact shown digest."""

    del output_json
    try:
        context = build_context(home=home)
        bound = None
        if scope == "project":
            if project_root is None:
                raise PolicyViolation("Project integration sync --project-root ister")
            bound = _bound_project_root(home, project_root)
        elif project_root is not None:
            raise PolicyViolation("User integration sync project root kabul etmez")
        plan = build_sync_plan(
            context,
            scope=scope,
            native_user_root=Path.home(),
            project_root=bound,
            enable=tuple(enable or ()),
            disable=tuple(disable or ()),
            registered_projects=(_registered_projects(home) if scope == "user" else ()),
        )
        if not apply:
            _emit(plan.as_dict())
            return
        if plan_digest is None:
            raise PolicyViolation("Integration sync --plan-digest ister")
        if plan_digest != plan.plan_digest:
            replayed = sync_receipt_by_digest(
                context,
                authorized_plan_digest=plan_digest,
                native_user_root=Path.home(),
                project_root=bound,
            )
            if (
                replayed.get("scope") != scope
                or replayed.get("after_policy") != plan.after_policy.body()
            ):
                raise PolicyViolation("Integration sync replay requested intent ile eslesmiyor")
            _emit(replayed)
            return
        _learning, lifecycle, resolved_home = _paths(home)
        effect: dict[str, object] = {
            "schema": "zekam-cli-integration-sync-effect/v1",
            "plan": plan.as_dict(),
        }
        mutation_resource = integration_mutation_resource(
            scope=plan.scope,
            native_user_root=plan.native_user_root,
            project_root=plan.project_root,
        )
        _emit(
            _run_claimed_effect(
                lifecycle,
                resolved_home,
                operation="integration.sync-v1",
                effect=effect,
                idempotency_key=f"integration-sync-v1:{plan_digest}",
                apply_effect=lambda: apply_sync_plan(plan, authorized_plan_digest=plan_digest),
                replay_effect=lambda: sync_receipt(plan, authorized_plan_digest=plan_digest),
                logical_resources=(mutation_resource,),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("rollback")
def rollback_command(
    receipt: Annotated[str, typer.Option("--receipt")],
    project_root: Annotated[
        Path | None, typer.Option("--project-root", exists=True, file_okay=False)
    ] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Plan or apply digest-bound rollback without overwriting user drift."""

    del output_json
    try:
        context = build_context(home=home)
        bound = None if project_root is None else _bound_project_root(home, project_root)
        plan = build_rollback_plan(
            context,
            receipt_id=receipt,
            native_user_root=Path.home(),
            project_root=bound,
        )
        if not apply:
            _emit(plan.as_dict())
            return
        if plan_digest is None:
            raise PolicyViolation("Integration rollback --plan-digest ister")
        _learning, lifecycle, resolved_home = _paths(home)
        effect: dict[str, object] = {
            "schema": "zekam-cli-integration-rollback-effect/v1",
            "plan": plan.as_dict(),
        }
        mutation_resource = integration_mutation_resource(
            scope=str(plan.receipt["scope"]),
            native_user_root=plan.native_user_root,
            project_root=plan.project_root,
        )
        _emit(
            _run_claimed_effect(
                lifecycle,
                resolved_home,
                operation="integration.rollback-v1",
                effect=effect,
                idempotency_key=f"integration-rollback-v1:{plan_digest}",
                apply_effect=lambda: apply_rollback_plan(plan, authorized_plan_digest=plan_digest),
                replay_effect=lambda: rollback_receipt(plan, authorized_plan_digest=plan_digest),
                logical_resources=(mutation_resource,),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
