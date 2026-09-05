"""Shared local-only CLI helpers."""

from __future__ import annotations

import typer

from zekam.application.composition import build_context
from zekam.domain.errors import NotFound, PolicyViolation, ZekamError
from zekam.domain.identity import PRODUCT
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.sqlite.repository import SQLitePersistence

EXIT_RUNTIME_ERROR = 70
EXIT_NOT_FOUND = 66
EXIT_AMBIGUOUS = 65
EXIT_POLICY_VIOLATION = 77
HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"
REALM_HELP = "Yerel realm; yalniz 'yerel' desteklenir"


def _default_realm(realm: str) -> None:
    if realm != DEFAULT_REALM_SLUG:
        raise PolicyViolation("Yerel profil yalniz varsayilan realm'i destekler")


def sqlite_repository(home: str | None, realm: str) -> SQLitePersistence | None:
    _default_realm(realm)
    context = build_context(home=home)
    return SQLitePersistence(context.settings.database.sqlite_path(context.home))


def sqlite_operational_store(home: str | None, realm: str) -> SQLiteOperationalStore | None:
    _default_realm(realm)
    context = build_context(home=home)
    return SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))


def fail(message: str, code: int = EXIT_RUNTIME_ERROR) -> typer.Exit:
    from rich.console import Console

    Console(stderr=True).print(f"[red]Hata:[/red] {message}")
    return typer.Exit(code)


def fail_from(exc: ZekamError) -> typer.Exit:
    code = (
        EXIT_NOT_FOUND
        if isinstance(exc, NotFound)
        else EXIT_POLICY_VIOLATION
        if isinstance(exc, PolicyViolation)
        else EXIT_RUNTIME_ERROR
    )
    return fail(str(exc), code)
