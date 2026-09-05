"""Read-only local configuration and permission-profile compatibility surface."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.config import load_settings
from zekam.application.home import resolve_home
from zekam.domain.config_provenance import builtin_permission_profiles
from zekam.domain.errors import ZekamError

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
    field: Annotated[str | None, typer.Argument()] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    try:
        settings = load_settings(home=resolve_home(home))
        graph = settings.config_provenance
        if graph is None:
            raise ZekamError("Config provenance uretilmedi")
        document = graph.body() if field is None else graph.explain(field).body()
        document |= {
            "effective_digest": graph.effective_digest,
            "graph_digest": graph.graph_digest,
            "read_only": True,
            "grants_authority": False,
        }
    except ZekamError as exc:
        error_console.print(str(exc))
        raise typer.Exit(70) from exc
    if output_json:
        console.print_json(json.dumps(document, default=str))
        return
    console.print(f"digest: {graph.graph_digest}")


@profile_app.command("explain")
def explain_permission_profile(
    name: Annotated[str, typer.Argument()],
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    matches = tuple(profile for profile in builtin_permission_profiles() if profile.name == name)
    if len(matches) != 1:
        error_console.print("Permission profile bulunamadi")
        raise typer.Exit(70)
    profile = matches[0]
    document = profile.body() | {
        "profile_digest": profile.profile_digest,
        "catalog_source": "builtin",
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, default=str))
        return
    console.print(f"{profile.name}@{profile.revision} {profile.profile_digest}")
