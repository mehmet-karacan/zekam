"""Global MCP registry and native client projection commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from zekam.application.atlassian_mcp_server import serve as serve_atlassian
from zekam.application.composition import build_context
from zekam.application.local_effects import run_claimed_local_effect
from zekam.application.mcp_integrations import (
    apply_sync_plan,
    build_register_plan,
    build_remove_plan,
    build_rollback_plan,
    build_sync_plan,
    mcp_mutation_resource,
    registry_status,
    sync_receipt,
)
from zekam.domain.client_integration import ClientIntegrationId
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.mcp_integration import McpServerRegistration, McpTransport
from zekam.interfaces.cli.session import HOME_HELP

app = typer.Typer(
    name="mcp",
    help="Kanonik MCP registry, native istemci sync ve yerel server yuzeyi",
    no_args_is_help=True,
)


def _emit(document: object) -> None:
    typer.echo(json.dumps(document, ensure_ascii=True, sort_keys=True, default=str))


def _registration(
    *,
    name: str,
    command: str | None,
    arguments: list[str] | None,
    url: str | None,
    env_vars: list[str] | None,
    bearer_token_env_var: str | None,
    clients: list[str] | None,
    disabled: bool,
) -> McpServerRegistration:
    selected = tuple(
        ClientIntegrationId(item)
        for item in (clients or [item.value for item in ClientIntegrationId])
    )
    if url is not None:
        if command is not None or arguments:
            raise PolicyViolation("HTTP MCP command/arg kabul etmez")
        return McpServerRegistration(
            name=name,
            transport=McpTransport.HTTP,
            url=url,
            env_vars=tuple(env_vars or ()),
            bearer_token_env_var=bearer_token_env_var,
            clients=selected,
            enabled=not disabled,
        )
    if command is None:
        raise PolicyViolation("STDIO MCP --command ister")
    return McpServerRegistration(
        name=name,
        transport=McpTransport.STDIO,
        command=(command, *(arguments or ())),
        env_vars=tuple(env_vars or ()),
        clients=selected,
        enabled=not disabled,
    )


@app.command("status")
def status_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    del output_json
    try:
        _emit(registry_status(build_context(home=home), native_user_root=Path.home()))
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("add")
def add_command(
    name: Annotated[str, typer.Argument()],
    command: Annotated[str | None, typer.Option("--command")] = None,
    argument: Annotated[list[str] | None, typer.Option("--arg")] = None,
    url: Annotated[str | None, typer.Option("--url")] = None,
    env_var: Annotated[list[str] | None, typer.Option("--env-var")] = None,
    bearer_token_env_var: Annotated[str | None, typer.Option("--bearer-token-env-var")] = None,
    client: Annotated[list[str] | None, typer.Option("--client")] = None,
    disabled: Annotated[bool, typer.Option("--disabled")] = False,
    adopt: Annotated[bool, typer.Option("--adopt")] = False,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    del output_json
    try:
        context = build_context(home=home)
        registration = _registration(
            name=name,
            command=command,
            arguments=argument,
            url=url,
            env_vars=env_var,
            bearer_token_env_var=bearer_token_env_var,
            clients=client,
            disabled=disabled,
        )
        plan = build_register_plan(context, registration, native_user_root=Path.home(), adopt=adopt)
        if not apply:
            _emit(plan.as_dict())
            return
        if plan_digest != plan.plan_digest:
            raise PolicyViolation("MCP add exact --plan-digest ister")
        effect_plan = plan.body() | {"plan_digest": plan.plan_digest}
        result = run_claimed_local_effect(
            context.settings.database.sqlite_path(context.home),
            context.home,
            operation="mcp.sync-v1",
            effect={"schema": "zekam-mcp-sync-effect/v1", "plan": effect_plan},
            idempotency_key=f"mcp-sync-v1:{plan.plan_digest}",
            apply_effect=lambda: apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest),
            replay_effect=lambda: sync_receipt(plan, authorized_plan_digest=plan.plan_digest),
            logical_resources=(mcp_mutation_resource(Path.home()),),
        )
        _emit(result)
    except (ValueError, ZekamError) as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("sync")
def sync_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    del output_json
    try:
        context = build_context(home=home)
        plan = build_sync_plan(context, native_user_root=Path.home())
        if not apply:
            _emit(plan.as_dict())
            return
        if plan_digest != plan.plan_digest:
            raise PolicyViolation("MCP sync exact --plan-digest ister")
        effect_plan = plan.body() | {"plan_digest": plan.plan_digest}
        _emit(
            run_claimed_local_effect(
                context.settings.database.sqlite_path(context.home),
                context.home,
                operation="mcp.sync-v1",
                effect={"schema": "zekam-mcp-sync-effect/v1", "plan": effect_plan},
                idempotency_key=f"mcp-sync-v1:{plan.plan_digest}",
                apply_effect=lambda: apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest),
                replay_effect=lambda: sync_receipt(plan, authorized_plan_digest=plan.plan_digest),
                logical_resources=(mcp_mutation_resource(Path.home()),),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("remove")
def remove_command(
    name: Annotated[str, typer.Argument()],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    del output_json
    try:
        context = build_context(home=home)
        plan = build_remove_plan(context, name, native_user_root=Path.home())
        if not apply:
            _emit(plan.as_dict())
            return
        if plan_digest != plan.plan_digest:
            raise PolicyViolation("MCP remove exact --plan-digest ister")
        effect_plan = plan.body() | {"plan_digest": plan.plan_digest}
        _emit(
            run_claimed_local_effect(
                context.settings.database.sqlite_path(context.home),
                context.home,
                operation="mcp.sync-v1",
                effect={"schema": "zekam-mcp-sync-effect/v1", "plan": effect_plan},
                idempotency_key=f"mcp-sync-v1:{plan.plan_digest}",
                apply_effect=lambda: apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest),
                replay_effect=lambda: sync_receipt(plan, authorized_plan_digest=plan.plan_digest),
                logical_resources=(mcp_mutation_resource(Path.home()),),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("rollback")
def rollback_command(
    receipt: Annotated[str, typer.Option("--receipt")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    del output_json
    try:
        context = build_context(home=home)
        plan = build_rollback_plan(context, receipt_digest=receipt, native_user_root=Path.home())
        if not apply:
            _emit(plan.as_dict())
            return
        if plan_digest != plan.plan_digest:
            raise PolicyViolation("MCP rollback exact --plan-digest ister")
        effect_plan = plan.body() | {"plan_digest": plan.plan_digest}
        _emit(
            run_claimed_local_effect(
                context.settings.database.sqlite_path(context.home),
                context.home,
                operation="mcp.sync-v1",
                effect={"schema": "zekam-mcp-sync-effect/v1", "plan": effect_plan},
                idempotency_key=f"mcp-sync-v1:{plan.plan_digest}",
                apply_effect=lambda: apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest),
                replay_effect=lambda: sync_receipt(plan, authorized_plan_digest=plan.plan_digest),
                logical_resources=(mcp_mutation_resource(Path.home()),),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("serve", hidden=True)
def serve_command(server: Annotated[str, typer.Argument()]) -> None:
    if server != "innova-atlassian":
        typer.echo("Hata: bilinmeyen built-in MCP server", err=True)
        raise typer.Exit(64)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    serve_atlassian()
