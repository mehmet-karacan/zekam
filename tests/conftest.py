"""Ortak test fixture'lari.

Testler gercek kullanici `ZEKAM_HOME` dizinine dokunmaz; her test kendi gecici kokunu
kullanir. PostgreSQL testleri yalnizca gercek sunucu erisilebilirse calisir.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from zekam.application.composition import ApplicationContext, build_context
from zekam.application.config import DatabaseSettings, Settings, load_settings
from zekam.application.home import HomeLayout

#: Testlerin sizdirmamasi gereken ortam degiskenleri.
_ISOLATED_ENV_KEYS = (
    "ZEKAM_HOME",
    "ZEKAM_DATABASE_BACKEND",
    "ZEKAM_DATABASE_HOST",
    "ZEKAM_DATABASE_PORT",
    "ZEKAM_DATABASE_NAME",
    "ZEKAM_DATABASE_USER",
    "ZEKAM_DATABASE_SSLMODE",
    "ZEKAM_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch: pytest.MonkeyPatch) -> Mapping[str, str]:
    """Zekam ortam degiskenlerinden arindirilmis ortam.

    Autouse'dur: operator kabuktan `ZEKAM_DATABASE_NAME` gibi bir degisken
    export etmisse CLI kabul testleri fixture veritabani yerine gercek
    gelistirme veritabanina yazar. ZEKAM-DEF-002 tam olarak bu yoldan olustu.
    """
    for key in _ISOLATED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return os.environ


@pytest.fixture
def home_root(tmp_path: Path) -> Path:
    """Gecici ZEKAM_HOME koku."""
    return tmp_path / "zekam-home"


@pytest.fixture
def layout(home_root: Path) -> HomeLayout:
    """Olusturulmus gecici yerlesim."""
    return HomeLayout(home_root).ensure()


@pytest.fixture
def settings(home_root: Path, clean_environ: Mapping[str, str]) -> Settings:
    """Gecici kok icin cozulmus ayarlar."""
    return load_settings(home=home_root, environ={})


@pytest.fixture
def context(
    home_root: Path,
    clean_environ: Mapping[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ApplicationContext]:
    """Gecici kok kullanan uygulama baglami."""
    monkeypatch.setenv("ZEKAM_HOME", str(home_root))
    yield build_context()


def _database_settings_from_env() -> DatabaseSettings | None:
    host = os.environ.get("ZEKAM_TEST_DATABASE_HOST")
    port = os.environ.get("ZEKAM_TEST_DATABASE_PORT")
    if not host or not port:
        return None
    return DatabaseSettings(
        host=host,
        port=int(port),
        name=os.environ.get("ZEKAM_TEST_DATABASE_NAME", "zekam"),
        user=os.environ.get("ZEKAM_TEST_DATABASE_USER", "zekam"),
        sslmode=os.environ.get("ZEKAM_TEST_DATABASE_SSLMODE", "prefer"),
    )


@pytest.fixture(scope="session")
def postgres_settings() -> DatabaseSettings:
    """Gercek PostgreSQL ayarlari; yoksa test atlanir."""
    resolved = _database_settings_from_env()
    if resolved is None:
        pytest.skip(
            "ZEKAM_TEST_DATABASE_HOST ve ZEKAM_TEST_DATABASE_PORT tanimli degil; "
            "PostgreSQL kabul testi atlandi"
        )
    return resolved


@pytest.fixture(scope="session")
def migrated_database(postgres_settings: DatabaseSettings) -> Iterator[DatabaseSettings]:
    """Migration'lari uygulanmis, yalitilmis gecici test veritabani.

    Gercek PostgreSQL uzerinde olusturulur ve oturum sonunda dusurulur. Boylece
    kabul testleri gelistirme verisine dokunmaz.
    """
    import secrets

    from zekam.infrastructure.postgres import migrations
    from zekam.infrastructure.postgres.connection import connect

    database_name = f"zekam_test_{secrets.token_hex(6)}"
    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute(f'create database "{database_name}"')

    scoped = DatabaseSettings(
        host=postgres_settings.host,
        port=postgres_settings.port,
        name=database_name,
        user=postgres_settings.user,
        sslmode=postgres_settings.sslmode,
    )
    try:
        # Eklentiler burada kurulmaz: migration'lar temiz kurulumda kendi kendine
        # yeterli olmalidir ve bu test bunu dogrular.
        with connect(scoped) as connection:
            migrations.upgrade(connection)
        yield scoped
    finally:
        with connect(postgres_settings) as connection, connection.cursor() as cursor:
            cursor.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (database_name,),
            )
            cursor.execute(f'drop database if exists "{database_name}"')


@pytest.fixture
def isolated_migrated_database(
    postgres_settings: DatabaseSettings,
) -> Iterator[DatabaseSettings]:
    """Tek teste ait, migration uygulanmis gecici PostgreSQL veritabani."""
    import secrets

    from zekam.infrastructure.postgres import migrations
    from zekam.infrastructure.postgres.connection import connect

    database_name = f"zekam_isolated_test_{secrets.token_hex(6)}"
    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute(f'create database "{database_name}"')

    scoped = DatabaseSettings(
        host=postgres_settings.host,
        port=postgres_settings.port,
        name=database_name,
        user=postgres_settings.user,
        sslmode=postgres_settings.sslmode,
    )
    try:
        with connect(scoped) as connection:
            migrations.upgrade(connection)
        yield scoped
    finally:
        with connect(postgres_settings) as connection, connection.cursor() as cursor:
            cursor.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (database_name,),
            )
            cursor.execute(f'drop database if exists "{database_name}"')


@pytest.fixture
def realm_session(migrated_database: DatabaseSettings) -> Iterator[tuple[Any, Any]]:
    """Yeni bir realm ve o realm kapsaminda calisan uygulama rolu oturumu."""
    from zekam.domain.realm import Realm
    from zekam.infrastructure.postgres.connection import configure_session, connect
    from zekam.infrastructure.postgres.core_repository import RealmRepository

    realm = Realm.create(slug=f"test-{secrets_hex()}", display_name="Test realm")
    with connect(migrated_database) as connection:
        configure_session(connection, realm_id=realm.id, role=None)
        RealmRepository(connection).create(realm)
        configure_session(connection, realm_id=realm.id)
        yield realm, connection


def secrets_hex() -> str:
    """Test slug'lari icin kisa rastgele son ek."""
    import secrets

    return secrets.token_hex(4)
