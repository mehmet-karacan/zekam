"""Platforma ozgu test atlama tablosunun butunlugu (capraz platform beklentisi yok)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.platform_skips import PLATFORM_SKIPS

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def _all_nodeids() -> list[tuple[str, str, str]]:
    return [
        (group, reason, nodeid)
        for group, reasons in PLATFORM_SKIPS.items()
        for reason, nodeids in reasons.items()
        for nodeid in nodeids
    ]


def test_registry_groups_are_the_two_supported_ones() -> None:
    assert set(PLATFORM_SKIPS) <= {"posix-mac-only", "needs-symlink"}


def test_every_registered_nodeid_points_to_an_existing_test_file_and_is_unique() -> None:
    entries = _all_nodeids()
    assert entries, "tablo bos olmamali"
    seen: set[str] = set()
    for _group, _reason, nodeid in entries:
        path, _, test_name = nodeid.partition("::")
        assert (ROOT / path).is_file(), f"olmayan test dosyasi: {path}"
        assert test_name.strip(), nodeid
        assert nodeid not in seen, f"tekrarli kayit: {nodeid}"
        seen.add(nodeid)


def test_every_reason_is_explicit_and_non_empty() -> None:
    for reasons in PLATFORM_SKIPS.values():
        for reason, nodeids in reasons.items():
            assert reason.strip() and nodeids


def test_windows_only_files_named_in_conftest_exist() -> None:
    for relative in (
        "e2e/test_cli_opencode_windows.py",
        "unit/test_local_journal_outbox_windows.py",
        "unit/test_windows_task_scheduler.py",
    ):
        assert (ROOT / "tests" / relative).is_file(), relative
