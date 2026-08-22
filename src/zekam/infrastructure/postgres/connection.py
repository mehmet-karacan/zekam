"""PostgreSQL baglanti yardimcilari.

`psycopg` opsiyonel bir bagimliliktir. Kurulu degilse capability acikca
`unsupported` olarak raporlanir; sessizce baska bir surucuye dusulmez.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.config import DatabaseSettings, database_password
from zekam.domain.errors import ConfigurationError

try:  # pragma: no cover - ortam bagimli import
    import psycopg

    PSYCOPG_AVAILABLE = True
    PSYCOPG_IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - ortam bagimli import
    psycopg = None  # type: ignore[assignment]
    PSYCOPG_AVAILABLE = False
    PSYCOPG_IMPORT_ERROR = type(exc).__name__


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """Baglanilan sunucunun sanitize edilmis kimligi."""

    server_version_num: int
    server_version: str
    database: str
    user: str
    extensions: dict[str, str]

    @property
    def major_version(self) -> int:
        return self.server_version_num // 10000


def require_psycopg() -> None:
    """`psycopg` yoksa acik hata verir."""
    if not PSYCOPG_AVAILABLE:
        raise ConfigurationError(
            "PostgreSQL surucusu kurulu degil; `pip install 'zekam[db]'` calistirin"
        )


@contextmanager
def connect(settings: DatabaseSettings, *, autocommit: bool = True) -> Iterator[Any]:
    """Kisa omurlu bir baglanti acar. Parola yalnizca cagri aninda cozulur."""
    require_psycopg()
    assert psycopg is not None
    dsn = settings.dsn(database_password())
    connection = psycopg.connect(dsn, autocommit=autocommit)
    try:
        yield connection
    finally:
        connection.close()


def read_server_info(connection: Any) -> ServerInfo:
    """Sunucu surumu ve yuklu eklentileri okur."""
    with connection.cursor() as cursor:
        cursor.execute(
            "select current_setting('server_version_num')::int,"
            " version(), current_database(), current_user"
        )
        row = cursor.fetchone()
        cursor.execute("select extname, extversion from pg_extension order by extname")
        extensions = dict(cursor.fetchall())
    return ServerInfo(
        server_version_num=int(row[0]),
        server_version=str(row[1]),
        database=str(row[2]),
        user=str(row[3]),
        extensions=extensions,
    )


#: Uygulama rolu. Superuser RLS'i atladigi icin islemler bu rol altinda calisir.
APPLICATION_ROLE = "zekam_app"

#: Oturum realm kimligini tasiyan PostgreSQL ayari.
REALM_SETTING = "zekam.realm_id"


def set_realm_setting(connection: Any, realm_id: UUID | str | None) -> None:
    """Oturumun realm kapsamini ayarlar. Rolu degistirmez."""
    with connection.cursor() as cursor:
        cursor.execute(
            "select set_config(%s, %s, false)",
            (REALM_SETTING, "" if realm_id is None else str(realm_id)),
        )


def configure_session(
    connection: Any,
    *,
    realm_id: UUID | str | None = None,
    role: str | None = APPLICATION_ROLE,
) -> None:
    """Oturumu realm kapsamina ve uygulama roluna baglar.

    Rol degistirildikten sonra RLS politikalari gercekten uygulanir; superuser
    baglantisi tek basina yalitim saglamaz.
    """
    require_psycopg()
    assert psycopg is not None
    from psycopg import sql as psycopg_sql

    with connection.cursor() as cursor:
        cursor.execute(
            "select set_config(%s, %s, false)",
            (REALM_SETTING, "" if realm_id is None else str(realm_id)),
        )
        if role is not None:
            cursor.execute(psycopg_sql.SQL("set role {}").format(psycopg_sql.Identifier(role)))


@contextmanager
def session(
    settings: DatabaseSettings,
    *,
    realm_id: UUID | str | None = None,
    role: str | None = APPLICATION_ROLE,
    autocommit: bool = True,
) -> Iterator[Any]:
    """Realm kapsamli, uygulama rolu altinda calisan baglanti acar."""
    with connect(settings, autocommit=autocommit) as connection:
        configure_session(connection, realm_id=realm_id, role=role)
        yield connection


def reset_role(connection: Any) -> None:
    """Bakim islemleri icin rolu baglanti sahibine dondurur."""
    with connection.cursor() as cursor:
        cursor.execute("reset role")
