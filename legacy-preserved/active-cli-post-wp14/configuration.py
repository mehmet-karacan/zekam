"""Read-only configuration provenance and permission profile commands."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.config import load_settings
from zekam.application.home import resolve_home
from zekam.application.local_runtime_boundary import (
    ConfigProvenanceRepository,
)
from zekam.domain.config_provenance import builtin_permission_profiles
from zekam.domain.errors import ZekamError
from zekam.domain.identity import PRODUCT
from zekam.interfaces.cli.session import RealmSession

HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"
config_app = typer.Typer(name="config", help="Yapilandirma provenance islemleri")
permission_app = typer.Typer(name="permission", help="Permission profile katalogu")
profile_app = typer.Typer(name="profile", help="Named permission profile islemleri")
permission_app.add_typer(profile_app)
console = Console()
error_console = Console(stderr=True)


@profile_app.command("list")
def list_permission_profiles(
    output_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Installed built-in permission profile catalogunu salt okunur listeler."""

    profiles = tuple(sorted(builtin_permission_profiles(), key=lambda item: item.name))
    document = {
        "schema": "zekam-permission-profile-list/v1",
        "profiles": [
            {
                "name": profile.name,
                "revision": profile.revision,
                "managed": profile.managed,
                "profile_digest": profile.profile_digest,
            }
            for profile in profiles
        ],
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, sort_keys=True))
        return
    for profile in profiles:
        console.print(f"{profile.name}@{profile.revision} {profile.profile_digest}")


@config_app.command("explain")
def explain_config(
    field: Annotated[str | None, typer.Argument(help="Opsiyonel dotted field path")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Effective config alaninin layer origin ve disabled adaylarini aciklar."""
    try:
        settings = load_settings(home=resolve_home(home))
        graph = settings.config_provenance
        if graph is None:
            raise RuntimeError("Config provenance uretilmedi")
        document = graph.body() if field is None else graph.explain(field).body()
        document = document | {
            "effective_digest": graph.effective_digest,
            "graph_digest": graph.graph_digest,
            "read_only": True,
            "grants_authority": False,
        }
    except (ZekamError, RuntimeError) as exc:
        error_console.print(str(exc))
        raise typer.Exit(70) from exc
    if output_json:
        console.print_json(json.dumps(document, default=str))
        return
    if field is None:
        console.print(f"layers: {' -> '.join(graph.layer_stack)}")
        console.print(f"fields: {len(graph.fields)}")
    else:
        console.print(f"{field}: {document['value']!r}")
        console.print(f"origin: {document['origin']}")
    console.print(f"digest: {graph.graph_digest}")


@profile_app.command("explain")
def explain_permission_profile(
    name: Annotated[str, typer.Argument(help="Named profile adi")],
    output_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[
        str | None,
        typer.Option("--realm", help="Realm katalogundaki en son revision"),
    ] = None,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Built-in veya realm katalogundaki named revision'i salt okunur aciklar."""
    catalog_source = "builtin"
    try:
        if realm is not None:
            with RealmSession(home, realm) as realm_context:
                profile = ConfigProvenanceRepository(
                    realm_context.connection, realm_context.realm_id
                ).latest_profile(name)
            catalog_source = "postgres"
        else:
            matches = tuple(
                profile for profile in builtin_permission_profiles() if profile.name == name
            )
            if len(matches) != 1:
                raise ZekamError("Permission profile bulunamadi")
            profile = matches[0]
    except ZekamError as exc:
        error_console.print(str(exc))
        raise typer.Exit(70) from exc
    document = profile.body() | {
        "profile_digest": profile.profile_digest,
        "catalog_source": catalog_source,
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, default=str))
        return
    console.print(f"{profile.name}@{profile.revision}")
    console.print(f"allowed: {', '.join(profile.allowed_capabilities) or '-'}")
    console.print(f"denied: {', '.join(profile.denied_capabilities) or '-'}")
    console.print(f"managed: {profile.managed}")
    console.print(f"digest: {profile.profile_digest}")
