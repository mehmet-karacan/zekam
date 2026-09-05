"""Ortak test fixture'lari.

Testler gercek kullanici `ZEKAM_HOME` dizinine dokunmaz; her test kendi gecici kokunu
kullanir. Aktif gorev boyunca legacy PostgreSQL erisimi collection baslamadan engellenir.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from zekam.application.composition import ApplicationContext, build_context
from zekam.application.config import Settings, load_settings
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
