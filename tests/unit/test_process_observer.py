from __future__ import annotations

import time
from dataclasses import dataclass

from zekam.infrastructure.process_observer import (
    BoundedProcessObserver,
    RawProcessSample,
    RawProcessScan,
)


@dataclass(frozen=True)
class FakeSource:
    scan_result: RawProcessScan

    def scan(self, *, limit: int, budget_ms: int) -> RawProcessScan:
        assert limit > 0
        assert budget_ms > 0
        return self.scan_result


def sample(
    pid: int,
    parent: int,
    name: str,
    *argv: str,
    created: float = 1_700_000_000.0,
) -> RawProcessSample:
    return RawProcessSample(
        pid=pid,
        parent_pid=parent,
        name=name,
        create_time=created + pid,
        status="running",
        argv=argv,
        cpu_percent=1.5,
        rss_bytes=4096,
    )


def test_real_cli_roots_exclude_wrappers_children_and_ui_server() -> None:
    scan = RawProcessScan(
        (
            sample(10, 1, "opencode.exe", "opencode"),
            sample(11, 10, "node.exe", "node", "opencode-worker"),
            sample(20, 1, "node.exe", "node", "codex-cli"),
            sample(21, 20, "python.exe", "python", "worker.py"),
            sample(30, 1, "claude.exe", "claude"),
            sample(40, 1, "zekam.exe", "zekam", "ui", "serve"),
        )
    )

    snapshot = BoundedProcessObserver(FakeSource(scan)).read()

    assert [(item.identity.pid, item.client.value) for item in snapshot.roots] == [
        (30, "claude"),
        (20, "codex"),
        (10, "opencode"),
    ]
    assert {item.identity.pid for item in snapshot.processes if not item.root} == {11, 21}
    assert 40 not in {item.identity.pid for item in snapshot.processes}


def test_pid_reuse_is_distinguished_by_create_time() -> None:
    first = BoundedProcessObserver(
        FakeSource(RawProcessScan((sample(42, 1, "codex.exe", "codex", created=1000),)))
    ).read()
    second = BoundedProcessObserver(
        FakeSource(RawProcessScan((sample(42, 1, "codex.exe", "codex", created=2000),)))
    ).read()

    assert first.roots[0].identity.key != second.roots[0].identity.key


def test_denied_and_vanished_are_explained_without_fake_processes() -> None:
    snapshot = BoundedProcessObserver(
        FakeSource(RawProcessScan((), access_denied=2, vanished=3))
    ).read()

    assert snapshot.available is True
    assert snapshot.roots == ()
    assert snapshot.access_denied == 2
    assert snapshot.vanished == 3


def test_missing_psutil_style_source_fails_closed() -> None:
    snapshot = BoundedProcessObserver(
        FakeSource(RawProcessScan((), available=False, detail="psutil-not-installed"))
    ).read()

    assert snapshot.available is False
    assert snapshot.detail == "psutil-not-installed"
    assert snapshot.processes == ()


def test_sixty_four_roots_with_bounded_children_stays_bounded_and_fast() -> None:
    samples: list[RawProcessSample] = []
    for root_index in range(64):
        root_pid = 10_000 + root_index * 100
        samples.append(sample(root_pid, 1, "codex.exe", "codex"))
        for child_index in range(24):
            samples.append(
                sample(
                    root_pid + child_index + 1,
                    root_pid,
                    "node.exe",
                    "node",
                    "codex-cli",
                )
            )
    observer = BoundedProcessObserver(
        FakeSource(RawProcessScan(tuple(samples))),
        max_processes=2048,
        max_children_per_root=16,
        budget_ms=2000,
    )

    started = time.perf_counter()
    snapshot = observer.read()
    duration_ms = (time.perf_counter() - started) * 1000

    assert len(snapshot.roots) == 64
    assert len(snapshot.processes) == 64 * 17
    assert all(
        sum(item.parent_identity_key == root.identity.key for item in snapshot.processes) == 16
        for root in snapshot.roots
    )
    assert duration_ms < 250
