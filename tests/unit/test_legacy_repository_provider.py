from __future__ import annotations

import subprocess
import sys


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
    )
    assert result.returncode == 0, result.stderr
