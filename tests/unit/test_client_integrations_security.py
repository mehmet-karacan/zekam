from __future__ import annotations

from pathlib import Path

import pytest

import zekam.application.client_integrations as client_integrations


def test_secret_scan_is_bounded_and_fails_closed_on_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.json"
    target.write_bytes(b"token: safe\n")
    monkeypatch.setattr(client_integrations, "MAX_SECRET_SCAN_BYTES", 4)

    assert client_integrations._contains_secret_assignment(target) is True


def test_secret_scan_rejects_unreadable_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.json"
    target.write_bytes(b"safe")

    def denied(_path: Path):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "lstat", denied)
    assert client_integrations._contains_secret_assignment(target) is True
