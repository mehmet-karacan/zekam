from __future__ import annotations

import sys
from unittest.mock import Mock

import pytest

from zekam.interfaces.cli import background


def test_background_entrypoint_dispatches_only_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    tick = Mock()
    monkeypatch.setattr("zekam.interfaces.cli.local_runtime.tick_command", tick)
    monkeypatch.setattr(sys, "argv", ["zekam-background", "tick", "--home", "C:\\data"])

    background.run()

    tick.assert_called_once_with(owner_id="zekam-os-supervisor", home="C:\\data")


def test_background_entrypoint_rejects_other_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["zekam-background", "doctor", "--home", "C:\\data"])

    with pytest.raises(SystemExit, match="2"):
        background.run()
