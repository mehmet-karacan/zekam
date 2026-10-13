from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Repo kokunun src/ dizini: src layout'te `zekam` site-packages'da kurulmamis
# olabilir, alt process PYTHONPATH'i bu kesin yola gosterilmeli.
_REPO_SRC = Path(__file__).resolve().parents[2] / "src"


def _subprocess_env() -> dict[str, str]:
    """Alt process orami: mevcut (temizlenmis) orami kopyala, PYTHONPATH onune src ekle.

    Ana process `os.environ`'i degistirilmez; yalnizca `subprocess.run(env=...)`
    araciligiyla bu kopya gecilir, boylece user ortami global olarak dokunulmaz.
    """
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_REPO_SRC}{os.pathsep}{existing}" if existing else str(_REPO_SRC)
    return env


def test_unconfigured_legacy_provider_fails_closed_in_clean_process() -> None:
    script = """
from uuid import UUID
from zekam.application.legacy_repository_provider import legacy_repository
from zekam.domain.errors import ConfigurationError
try:
    legacy_repository('job', object(), UUID(int=1))
except ConfigurationError as exc:
    assert 'composition root' in str(exc)
else:
    raise AssertionError('unconfigured provider accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr


def test_provider_install_is_idempotent_but_engine_switch_is_rejected() -> None:
    script = """
from zekam.application.legacy_repository_provider import install_legacy_repository_provider
from zekam.domain.errors import ConfigurationError
class First:
    def build(self, *args, **kwargs): return object()
    def maintain(self, *args, **kwargs): return object()
class Second(First): pass
install_legacy_repository_provider(First())
install_legacy_repository_provider(First())
try:
    install_legacy_repository_provider(Second())
except ConfigurationError as exc:
    assert 'degistirilemez' in str(exc)
else:
    raise AssertionError('provider engine switch accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr
