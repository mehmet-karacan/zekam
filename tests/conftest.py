"""Ortak test fixture'lari.

Testler gercek kullanici `ZEKAM_HOME` dizinine dokunmaz; her test kendi gecici kokunu
kullanir. Aktif gorev boyunca legacy PostgreSQL erisimi collection baslamadan engellenir.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from tests.platform_skips import PLATFORM_SKIPS

from zekam.application.composition import ApplicationContext, build_context
from zekam.application.config import Settings, load_settings
from zekam.application.home import HomeLayout

# Legacy POSIX continuity slices are retained for historical recovery work but
# are outside the Windows release test surface and are never collected.
collect_ignore: list[str] = [
    "unit/test_wp16_continuity_spool_exact_missing_coverage.py",
]

# Simetrik kural: POSIX cihazlarda Windows'a ozgu testler beklenmez. Bu dosyalar
# yalniz Windows API'lerini sinar.
if sys.platform != "win32":
    collect_ignore += [
        "e2e/test_cli_opencode_windows.py",
        "unit/test_local_journal_outbox_windows.py",
        "unit/test_windows_task_scheduler.py",
    ]


def _can_create_symlink() -> bool:
    """Bu makinede (ayricalik/Developer Mode dahil) symlink kurulabilir mi?"""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "target"
        target.write_text("x", encoding="utf-8")
        try:
            (Path(directory) / "link").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
    return True


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Platforma ozgu testleri uyumsuz platformda atlar (cross-platform beklenmez).

    Veri `tests/platform_skips.py` icindedir; her nodeid neden etiketiyle tutulur. Uyumlu
    platformda (Windows'ta posix-only, symlink yapilabilen makinede needs-symlink)
    hicbir sey atlanmaz.
    """
    skip_posix = sys.platform == "win32"
    skip_symlink = not _can_create_symlink()
    reasons: dict[str, str] = {}
    if skip_posix:
        for reason, nodeids in PLATFORM_SKIPS["posix-only"].items():
            reasons.update(dict.fromkeys(nodeids, f"POSIX'e ozgu: {reason}"))
    if skip_symlink:
        for reason, nodeids in PLATFORM_SKIPS["needs-symlink"].items():
            reasons.update(dict.fromkeys(nodeids, f"bu makinede {reason}"))
    if not reasons:
        return
    for item in items:
        reason = reasons.get(item.nodeid)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


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
