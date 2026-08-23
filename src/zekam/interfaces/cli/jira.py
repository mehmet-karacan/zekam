"""Jira issue key cozumleme CLI yuzeyi."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.jira_issue_routing import resolve_jira_issue
from zekam.domain.errors import ZekamError
from zekam.interfaces.cli.session import fail_from

app = typer.Typer(name="jira", help="Jira proje ve issue yonlendirmesi", no_args_is_help=True)
console = Console()


@app.command("resolve")
def resolve_command(
    query: Annotated[str, typer.Argument(help="Proje aliasi + task sayisi veya exact issue key")],
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Dogal ifadeyi exact Jira issue key'e salt okunur cozer."""
    try:
        resolution = resolve_jira_issue(query)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if as_json:
        console.print_json(json.dumps(resolution.as_dict(), ensure_ascii=False))
    else:
        console.print(resolution.issue_key)
