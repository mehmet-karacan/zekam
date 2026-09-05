from __future__ import annotations

import ctypes

from benchmarks.suites import wp01_platform


def test_exact_windows_x64_is_classified(monkeypatch) -> None:
    monkeypatch.setattr(wp01_platform.sys, "platform", "win32")
    monkeypatch.setattr(wp01_platform.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(ctypes, "sizeof", lambda _value: 8)
    assert wp01_platform.current_acceptance_platform() == "windows-x64"


def test_unsupported_windows_architecture_is_not_claimed(monkeypatch) -> None:
    monkeypatch.setattr(wp01_platform.sys, "platform", "win32")
    monkeypatch.setattr(wp01_platform.platform, "machine", lambda: "ARM64")
    assert wp01_platform.current_acceptance_platform() is None
