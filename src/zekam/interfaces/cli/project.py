"""Local operational project registry commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from zekam.domain.errors import ZekamError
from zekam.domain.identifiers import normalize_slug, validate_slug
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.interfaces.cli.session import (
    HOME_HELP,
    REALM_HELP,
    fail,
    fail_from,
    sqlite_operational_store,
)

app = typer.Typer(name="project", help="Yerel proje kayitlari", no_args_is_help=True)
console = Console()


@app.command("add")
def add_command(
    source: Annotated[Path, typer.Argument(help="Kaynak proje kok dizini")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    slug: Annotated[str | None, typer.Option("--slug")] = None,
    alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    resolved = source.expanduser()
    if not resolved.is_dir():
        raise fail("Kaynak koku bir dizin olmali")
    selected_slug = validate_slug(slug) if slug else normalize_slug(resolved.name)
    if not apply:
        console.print_json(
            json.dumps({"slug": selected_slug, "source_kind": "read-only", "apply": False})
        )
        return
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project = uow.create_project(slug=selected_slug, display_name=name or resolved.name)
            for item in alias or ():
                uow.add_project_alias(project_id=project.id, alias=item)
            uow.bind_source(
                project_id=project.id,
                portable_ref=f"source:{selected_slug}",
                source_kind="git" if (resolved / ".git").is_dir() else "directory",
            )
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Kaydedildi:[/green] {project.slug} ({project.id})")


@app.command("list")
def list_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    include_archived: Annotated[bool, typer.Option("--include-archived")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            rows = [
                {
                    "id": item.id,
                    "slug": item.slug,
                    "display_name": item.display_name,
                    "status": item.status,
                    "revision": item.revision,
                    "aliases": list(uow.list_project_aliases(item.id)),
                }
                for item in uow.list_projects(include_archived=include_archived)
            ]
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(rows, ensure_ascii=False))
    else:
        for row in rows:
            console.print(f"{row['slug']}\t{row['display_name']}\t{row['status']}")
