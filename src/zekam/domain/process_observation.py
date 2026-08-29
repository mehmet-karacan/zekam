"""Content-free operating-system process observations for the local UI."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed


class ObservedClient(StrEnum):
    OPENCODE = "opencode"
    CODEX = "codex"
    CLAUDE = "claude"
    ZEKAM = "zekam"


class ProcessAvailability(StrEnum):
    LIVE = "live"
    WAITING = "waiting"
    UNBOUND = "unbound"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    create_time_micros: int

    def __post_init__(self) -> None:
        if self.pid <= 0 or self.create_time_micros <= 0:
            raise ValidationFailed("Process identity pozitif pid ve create_time ister")

    @property
    def key(self) -> str:
        return f"process:{self.pid}:{self.create_time_micros}"


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    identity: ProcessIdentity
    parent_pid: int | None
    client: ObservedClient
    executable: str
    status: str
    started_at: dt.datetime
    cpu_percent: float | None = None
    rss_bytes: int | None = None
    root: bool = True
    parent_identity_key: str | None = None

    def __post_init__(self) -> None:
        if not self.executable or "/" in self.executable or "\\" in self.executable:
            raise ValidationFailed("Process executable yalniz basename olmali")
        if self.cpu_percent is not None and self.cpu_percent < 0:
            raise ValidationFailed("CPU yuzdesi negatif olamaz")
        if self.rss_bytes is not None and self.rss_bytes < 0:
            raise ValidationFailed("RSS negatif olamaz")

    def safe_body(self) -> dict[str, object]:
        return {
            "process_id": self.identity.key,
            "pid": self.identity.pid,
            "create_time_micros": self.identity.create_time_micros,
            "parent_pid": self.parent_pid,
            "client": self.client.value,
            "executable": self.executable,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "cpu_percent": self.cpu_percent,
            "rss_bytes": self.rss_bytes,
            "root": self.root,
            "parent_process_id": self.parent_identity_key,
        }


@dataclass(frozen=True, slots=True)
class ProcessObservationSnapshot:
    observed_at: dt.datetime
    processes: tuple[ProcessObservation, ...]
    available: bool
    detail: str
    truncated: bool = False
    access_denied: int = 0
    vanished: int = 0

    @property
    def roots(self) -> tuple[ProcessObservation, ...]:
        return tuple(item for item in self.processes if item.root)

    @property
    def source_digest(self) -> str:
        return digest(
            {
                "processes": [item.safe_body() for item in self.processes],
                "available": self.available,
                "detail": self.detail,
                "truncated": self.truncated,
                "access_denied": self.access_denied,
                "vanished": self.vanished,
            }
        )
