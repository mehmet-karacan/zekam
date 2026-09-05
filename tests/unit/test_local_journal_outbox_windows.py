"""Windows-native journal/outbox locking, ACL and reparse regressions."""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zekam.application.local_runtime import LocalOutboxClaim, LocalOutboxEvent
from zekam.application.local_runtime_service import LocalEffectRequest
from zekam.domain.canonical import digest
from zekam.domain.errors import ZekamError
from zekam.infrastructure.local_file_security import private_regular, restrict_private_tree
from zekam.infrastructure.local_runtime_effects import (
    LocalJournalEffectExecutor,
    LocalJournalOutboxPublisher,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != "nt", reason="Windows-native journal acceptance"),
]


def _claim() -> LocalOutboxClaim:
    payload = {"job_id": "windows-journal"}
    return LocalOutboxClaim(
        LocalOutboxEvent(
            "outbox-windows",
            "job-windows",
            "job:windows:enqueued",
            "job.enqueued",
            digest(payload),
            payload,
            "claimed",
        ),
        "claim-windows",
        "windows-owner",
        os.getpid(),
        "windows-token",
        1,
        "2099-01-01T00:00:00+00:00",
    )


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime" / "local-effects"
    root.mkdir(parents=True)
    restrict_private_tree(root)
    return root


def test_windows_publisher_and_effect_use_private_regular_journals(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    claim = _claim()
    assert LocalJournalOutboxPublisher(root)(claim).status == "delivered"
    effect = LocalEffectRequest(
        "local.append-journal/v1",
        "windows-effect",
        {"relative_path": "nested/events.log", "line": "one"},
    )
    assert LocalJournalEffectExecutor(root)(effect).status == "completed"
    assert private_regular(root / "outbox-delivery.journal")
    assert private_regular(root / "nested" / "events.log")


def test_windows_hardlink_cannot_mutate_outside_victim(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    victim = tmp_path / "outside-victim"
    victim.write_bytes(b"preserve\n")
    os.link(victim, root / "outbox-delivery.journal")
    with pytest.raises((ZekamError, OSError)):
        LocalJournalOutboxPublisher(root)(_claim())
    assert victim.read_bytes() == b"preserve\n"


def test_windows_junction_parent_cannot_escape_runtime_root(tmp_path: Path) -> None:
    victim = tmp_path / "outside"
    victim.mkdir()
    marker = victim / "outbox-delivery.journal"
    marker.write_bytes(b"preserve\n")
    root = tmp_path / "journal-junction"
    command = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "cmd.exe"
    created = subprocess.run(
        [str(command), "/d", "/c", "mklink", "/J", str(root), str(victim)],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert created.returncode == 0, created.stderr
    with pytest.raises((ZekamError, OSError)):
        LocalJournalOutboxPublisher(root)(_claim())
    assert marker.read_bytes() == b"preserve\n"


def test_windows_explicit_everyone_acl_is_rejected(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    target = root / "outbox-delivery.journal"
    target.write_bytes(b"prior\n")
    restrict_private_tree(root)
    icacls = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "icacls.exe"
    changed = subprocess.run(
        [str(icacls), str(target), "/grant", "*S-1-1-0:(R)", "/Q"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert changed.returncode == 0, changed.stderr
    with pytest.raises((ZekamError, OSError)):
        LocalJournalOutboxPublisher(root)(_claim())
    assert target.read_bytes() == b"prior\n"


def test_windows_second_writer_fails_while_region_lock_is_held(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    slow = LocalJournalOutboxPublisher(root, pause_after_write_ms=500)
    competing = LocalJournalOutboxPublisher(root)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(slow, _claim())
        target = root / "outbox-delivery.journal"
        deadline = time.monotonic() + 3
        while (not target.exists() or target.stat().st_size == 0) and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises((ZekamError, OSError)):
            competing(_claim())
        assert first.result(timeout=3).status == "delivered"
    assert competing(_claim()).status == "delivered"
