"""`zekam research` canonical run/status/report surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.opencode_embedding import default_opencode_config_file
from zekam.application.research_runtime import (
    build_research_run_plan,
    research_report,
    research_status,
    run_research,
)
from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, fail_from, sqlite_operational_store

app = typer.Typer(name="research", help="Kanitli project research islemleri", no_args_is_help=True)
console = Console()
_DEFAULT_OPENCODE_CONFIG_FILE = default_opencode_config_file()


def _runtime(home: Path) -> SQLiteLocalRuntimeStore:
    return SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)


@app.command("run")
def run_command(
    question: Annotated[str, typer.Argument(help="Arastirma sorusu")],
    project: Annotated[str, typer.Option("--project", help="Project slug, alias veya UUID")],
    run_digest: Annotated[
        str | None,
        typer.Option("--run-digest", help="Dry-run'da gosterilen exact plan digest'i"),
    ] = None,
    apply: Annotated[bool, typer.Option("--uygula", help="Plani claim ederek calistir")] = False,
    authorize_remote_query: Annotated[
        bool,
        typer.Option("--authorize-remote-query", help="Tek remote query disclosure yetkisi"),
    ] = False,
    authorize_agent_run: Annotated[
        bool,
        typer.Option("--authorize-agent-run", help="Bounded OpenCode agent run yetkisi"),
    ] = False,
    opencode_config: Annotated[
        Path,
        typer.Option("--opencode-config", help="OpenCode provider config yolu"),
    ] = _DEFAULT_OPENCODE_CONFIG_FILE,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Planı gösterir; explicit digest ve iki authorization olmadan effect yapmaz."""

    try:
        context = build_context(home=home)
        store = sqlite_operational_store(home, realm)
        assert store is not None
        plan = build_research_run_plan(
            store,
            context.home,
            project_ref=project,
            question=question,
        )
        if not apply:
            document = plan.body
        else:
            document = run_research(
                store,
                context.home,
                plan,
                expected_run_digest=run_digest or "",
                authorize_remote_query=authorize_remote_query,
                authorize_agent_run=authorize_agent_run,
                opencode_config=opencode_config,
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    elif not apply:
        console.print(f"run_digest: {document['run_digest']}")
        console.print(f"project: {document['project_slug']}")
        console.print("Dry-run; calistirmak icin exact digest ve iki authorization verin.")
    else:
        console.print(f"[green]{document['state']}[/green] {document['job_id']}")


@app.command("status")
def status_command(
    reference: Annotated[str, typer.Argument(help="Research job UUID veya idempotency key")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Durable job/effect/receipt durumunu salt okunur gösterir."""

    try:
        context = build_context(home=home)
        sqlite_operational_store(home, realm)
        document = research_status(_runtime(context.home), reference)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"{document['state']}\t{document['job_id']}\t{document['projection_ref']}")


@app.command("report")
def report_command(
    reference: Annotated[str, typer.Argument(help="Research job UUID veya idempotency key")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Terminal receipt ve Markdown digest'i eşleşen kanonik raporu gösterir."""

    try:
        context = build_context(home=home)
        store = sqlite_operational_store(home, realm)
        assert store is not None
        document = research_report(
            _runtime(context.home),
            store,
            KnowledgeFileStore(context.home),
            reference,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print_json(json.dumps(document["report"], ensure_ascii=False))
