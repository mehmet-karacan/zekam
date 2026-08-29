"""Bounded, cross-platform psutil adapter with content-free output."""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from pathlib import PurePath
from typing import Protocol

from zekam.domain.process_observation import (
    ObservedClient,
    ProcessIdentity,
    ProcessObservation,
    ProcessObservationSnapshot,
)


@dataclass(frozen=True, slots=True)
class RawProcessSample:
    pid: int
    parent_pid: int | None
    name: str
    create_time: float
    status: str
    argv: tuple[str, ...] = ()
    cpu_percent: float | None = None
    rss_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class RawProcessScan:
    samples: tuple[RawProcessSample, ...]
    truncated: bool = False
    access_denied: int = 0
    vanished: int = 0
    available: bool = True
    detail: str = "os-process-scan"


class ProcessSampleSource(Protocol):
    def scan(self, *, limit: int, budget_ms: int) -> RawProcessScan: ...


@dataclass(frozen=True, slots=True)
class PsutilProcessSource:
    """Read only the minimum process fields; argv is never returned by the API."""

    def scan(self, *, limit: int, budget_ms: int) -> RawProcessScan:
        try:
            import psutil
        except ImportError:
            return RawProcessScan((), available=False, detail="psutil-not-installed")
        started = time.monotonic()
        samples: list[RawProcessSample] = []
        denied = 0
        vanished = 0
        truncated = False
        attrs = ("pid", "name")
        wrapper_names = {
            "bun",
            "bun.exe",
            "cmd.exe",
            "node",
            "node.exe",
            "powershell.exe",
            "pwsh",
            "pwsh.exe",
            "python",
            "python.exe",
            "pythonw.exe",
        }
        inspected = 0
        for process in psutil.process_iter(attrs=attrs, ad_value=None):
            inspected += 1
            if inspected > limit or (time.monotonic() - started) * 1000 >= budget_ms:
                truncated = True
                break
            try:
                info = process.info
                name = str(info.get("name") or "unknown")
                lowered_name = name.casefold()
                if lowered_name not in wrapper_names and not any(
                    marker in lowered_name for marker in ("opencode", "codex", "claude", "zekam")
                ):
                    continue
                argv = (
                    tuple(str(value) for value in process.cmdline()[:12])
                    if lowered_name in wrapper_names
                    else ()
                )
                draft = RawProcessSample(
                    pid=int(info["pid"]),
                    parent_pid=int(process.ppid()),
                    name=name,
                    create_time=float(process.create_time()),
                    status=str(process.status()),
                    argv=argv,
                )
                if classify_client(draft) is None:
                    continue
                memory = process.memory_info()
                draft = RawProcessSample(
                    pid=draft.pid,
                    parent_pid=draft.parent_pid,
                    name=draft.name,
                    create_time=draft.create_time,
                    status=draft.status,
                    argv=draft.argv,
                    cpu_percent=float(process.cpu_percent(interval=None)),
                    rss_bytes=int(memory.rss),
                )
                samples.append(draft)
            except psutil.AccessDenied:
                denied += 1
            except (psutil.NoSuchProcess, psutil.ZombieProcess, KeyError, TypeError, ValueError):
                vanished += 1
        return RawProcessScan(tuple(samples), truncated, denied, vanished)


@dataclass(frozen=True, slots=True)
class BoundedProcessObserver:
    source: ProcessSampleSource = PsutilProcessSource()
    max_processes: int = 512
    max_children_per_root: int = 16
    budget_ms: int = 500

    def __post_init__(self) -> None:
        if not 1 <= self.max_processes <= 2048:
            raise ValueError("max_processes 1..2048 araliginda olmali")
        if not 0 <= self.max_children_per_root <= 64:
            raise ValueError("max_children_per_root 0..64 araliginda olmali")
        if not 10 <= self.budget_ms <= 2000:
            raise ValueError("budget_ms 10..2000 araliginda olmali")

    def read(self) -> ProcessObservationSnapshot:
        now = dt.datetime.now(dt.UTC)
        scan = self.source.scan(limit=self.max_processes, budget_ms=self.budget_ms)
        if not scan.available:
            return ProcessObservationSnapshot(
                now, (), False, scan.detail, scan.truncated, scan.access_denied, scan.vanished
            )
        classified = {
            sample.pid: client
            for sample in scan.samples
            if (client := classify_client(sample)) is not None
        }
        by_pid = {sample.pid: sample for sample in scan.samples}
        roots = tuple(
            sample
            for sample in scan.samples
            if sample.pid in classified
            and classified.get(sample.parent_pid or -1) != classified[sample.pid]
        )
        observations: list[ProcessObservation] = []
        for root in roots:
            client = classified[root.pid]
            root_identity = identity(root)
            observations.append(observation(root, client, root=True))
            children = tuple(
                sample
                for sample in scan.samples
                if sample.parent_pid == root.pid and sample.pid in by_pid
            )[: self.max_children_per_root]
            for child in children:
                observations.append(
                    observation(child, client, root=False, parent_identity_key=root_identity.key)
                )
        observations.sort(key=lambda item: (not item.root, item.client.value, item.identity.key))
        return ProcessObservationSnapshot(
            now,
            tuple(observations),
            True,
            "os-process-scan",
            scan.truncated,
            scan.access_denied,
            scan.vanished,
        )


def identity(sample: RawProcessSample) -> ProcessIdentity:
    return ProcessIdentity(sample.pid, max(1, int(sample.create_time * 1_000_000)))


def observation(
    sample: RawProcessSample,
    client: ObservedClient,
    *,
    root: bool,
    parent_identity_key: str | None = None,
) -> ProcessObservation:
    created = max(sample.create_time, 1.0)
    executable = PurePath(sample.name).name[:64] or "unknown"
    return ProcessObservation(
        identity=identity(sample),
        parent_pid=sample.parent_pid,
        client=client,
        executable=executable,
        status=safe_status(sample.status),
        started_at=dt.datetime.fromtimestamp(created, tz=dt.UTC),
        cpu_percent=sample.cpu_percent,
        rss_bytes=sample.rss_bytes,
        root=root,
        parent_identity_key=parent_identity_key,
    )


def classify_client(sample: RawProcessSample) -> ObservedClient | None:
    """Classify from bounded local metadata without retaining raw command lines."""

    name = PurePath(sample.name).name.casefold()
    tokens = tuple(PurePath(value).name.casefold() for value in sample.argv[:12])
    material = (name, *tokens)
    if any("opencode" in value for value in material):
        return ObservedClient.OPENCODE
    if any(
        value == "codex" or value.startswith("codex.") or "codex-cli" in value for value in material
    ):
        return ObservedClient.CODEX
    if any(
        value == "claude" or value.startswith("claude.") or "claude-code" in value
        for value in material
    ):
        return ObservedClient.CLAUDE
    if any(value == "zekam" or value.startswith("zekam.") for value in material):
        if "ui" in tokens and "serve" in tokens:
            return None
        return ObservedClient.ZEKAM
    return None


def safe_status(value: str) -> str:
    normalized = value.casefold().replace("-", "_")
    return (
        normalized
        if normalized in {"running", "sleeping", "idle", "waiting", "stopped"}
        else "unknown"
    )
