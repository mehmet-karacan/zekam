"""Deterministic intent + project-family routing CLI."""

from __future__ import annotations

import json
from typing import Annotated, cast

import typer
from rich.console import Console

from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
    route_request,
)
from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, fail_from, sqlite_operational_store

app = typer.Typer(name="route", help="Intent ve proje ailesi yonlendirmesi", no_args_is_help=True)
console = Console()


def _route(question: str, *, home: str | None, realm: str) -> dict[str, object]:
    store = sqlite_operational_store(home, realm)
    assert store is not None
    with store.unit_of_work() as uow:
        projects = tuple(
            RegisteredProject(item.slug, uow.list_project_aliases(item.id))
            for item in uow.list_projects(include_archived=False)
        )
        uow.commit()
    return route_request(
        question,
        catalog=load_project_families(),
        registered_projects=projects,
    ).as_dict()


@app.command("families")
def families_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Reviewed family/alias/role catalogue and live registry availability."""

    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            available = {item.slug for item in uow.list_projects(include_archived=False)}
            uow.commit()
        catalog = load_project_families()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    document: dict[str, object] = {
        "schema": "zekam-project-family-list/v1",
        "catalog_digest": catalog.catalog_digest,
        "families": [
            {
                "family_ref": family.family_ref,
                "display_name": family.display_name,
                "aliases": list(family.aliases),
                "jira_prefix": family.jira_prefix,
                "members": [
                    {
                        "project_ref": member.project_ref,
                        "role": member.role,
                        "available": member.project_ref in available,
                    }
                    for member in family.members
                ],
            }
            for family in catalog.families
        ],
        "provider_calls": 0,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        for family in cast(list[dict[str, object]], document["families"]):
            assert isinstance(family, dict)
            console.print(f"{family['family_ref']}\t{family['display_name']}")


@app.command("preview")
def preview_command(
    question: Annotated[str, typer.Argument(help="Exact kullanici sorusu")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Provider/source cagrisi yapmadan route kararini uretir."""

    try:
        document = _route(question, home=home, realm=realm)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        targets = (
            ",".join(str(item) for item in cast(tuple[str, ...], document["project_refs"])) or "-"
        )
        console.print(
            f"{document['status']}\t{document['intent']}\t{document['strategy']}\t{targets}"
        )


@app.command("explain")
def explain_command(
    question: Annotated[str, typer.Argument(help="Exact kullanici sorusu")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Preview ile ayni deterministik karari neden kodlariyla aciklar."""

    preview_command(question, output_json=output_json, realm=realm, home=home)
