"""CLI icin ortak realm oturumu ve hata kodu esleme.

Butun komutlar ayni baglanti/rol/realm kurulumunu kullanir; yuzey kendi kuralini
tanimlamaz.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import TracebackType

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.config import PersistenceBackend
from zekam.application.realm_context import RealmContext, attach_realm
from zekam.domain.errors import NotFound, ZekamError
from zekam.domain.identity import PRODUCT
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.postgres.connection import connect
from zekam.infrastructure.sqlite.repository import SQLitePersistence

#: Kararli cikis kodlari.
EXIT_RUNTIME_ERROR = 70
EXIT_NOT_FOUND = 4
EXIT_AMBIGUOUS = 5
EXIT_POLICY_VIOLATION = 6

HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"
REALM_HELP = "Kullanilacak realm slug'i"

error_console = Console(stderr=True)


class RealmSession:
    """Realm kapsamli, uygulama rolu altinda calisan CLI oturumu.

    Baglanti context manager'i acikca tutulur; aksi halde uretici nesne toplanir
    ve baglanti beklenmedik sekilde kapanir.
    """

    def __init__(self, home: str | None, realm: str, *, create_realm: bool = False) -> None:
        self._context = build_context(home=home)
        self._realm_slug = realm
        self._create_realm = create_realm
        self._stack = ExitStack()

    def __enter__(self) -> RealmContext:
        if self._context.settings.database.backend is PersistenceBackend.SQLITE:
            raise ZekamError(
                "Bu komut SQLite minimum profilinde desteklenmiyor; PostgreSQL'e fallback yok"
            )
        connection = self._stack.enter_context(connect(self._context.settings.database))
        return attach_realm(connection, slug=self._realm_slug, create_if_missing=self._create_realm)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Exception bilgisini connection context'ine iletmek transaction rollback'i icin
        # zorunludur. ``close()`` her cikisi basarili gibi gosterip kismi commit uretebilir.
        self._stack.__exit__(exc_type, exc, traceback)


def sqlite_repository(home: str | None, realm: str) -> SQLitePersistence | None:
    """SQLite seciliyse minimum repository'yi verir; aksi halde PG akisina birakir."""
    context = build_context(home=home)
    if context.settings.database.backend is PersistenceBackend.POSTGRESQL:
        return None
    if realm != DEFAULT_REALM_SLUG:
        raise ZekamError("SQLite minimum profili yalniz varsayilan realm'i destekler")
    return SQLitePersistence(context.settings.database.sqlite_path(context.home))


def fail(message: str, code: int = EXIT_RUNTIME_ERROR) -> typer.Exit:
    """Sanitize edilmis hata yazar ve kararli cikis kodu uretir."""
    error_console.print(f"[red]Hata:[/red] {message}")
    return typer.Exit(code)


def fail_from(exc: ZekamError) -> typer.Exit:
    """Hata turune gore kararli cikis kodu uretir."""
    from zekam.domain.errors import PolicyViolation

    if isinstance(exc, NotFound):
        code = EXIT_NOT_FOUND
    elif isinstance(exc, PolicyViolation):
        code = EXIT_POLICY_VIOLATION
    else:
        code = EXIT_RUNTIME_ERROR
    return fail(str(exc), code)
