"""Sticky environment cache and cache-bypassing live probe orchestration."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_environment import (
    EnvironmentDriftReport,
    ExecutionEnvironmentSnapshot,
    detect_environment_drift,
)


class EnvironmentProbe(Protocol):
    def probe(self, execution_identity: str, *, force: bool) -> ExecutionEnvironmentSnapshot: ...


class EnvironmentSnapshotStore(Protocol):
    def create_environment_snapshot(
        self, snapshot: ExecutionEnvironmentSnapshot
    ) -> tuple[object, bool]: ...

    def record_environment_probe(self, report: EnvironmentDriftReport) -> tuple[object, bool]: ...

    def environment_for_envelope(self, envelope_id: UUID) -> ExecutionEnvironmentSnapshot: ...


class EnvironmentEffectGuard(Protocol):
    def assert_envelope_current(
        self, envelope_id: UUID, *, now: dt.datetime
    ) -> ExecutionEnvironmentSnapshot: ...


@dataclass(slots=True)
class EnvironmentSnapshotService:
    probe_adapter: EnvironmentProbe
    repository: EnvironmentSnapshotStore
    _sticky: dict[str, ExecutionEnvironmentSnapshot] = field(default_factory=dict)

    def initialize(self, execution_identity: str) -> ExecutionEnvironmentSnapshot:
        cached = self._sticky.get(execution_identity)
        if cached is not None:
            return cached
        snapshot = self.probe_adapter.probe(execution_identity, force=False)
        if snapshot.execution_identity != execution_identity:
            raise PolicyViolation("Initialize probe execution identity drift")
        self.repository.create_environment_snapshot(snapshot)
        self._sticky[execution_identity] = snapshot
        return snapshot

    def sticky(self, execution_identity: str) -> ExecutionEnvironmentSnapshot:
        try:
            return self._sticky[execution_identity]
        except KeyError as exc:
            raise PolicyViolation("Execution environment initialize edilmedi") from exc

    def attach_sticky(self, snapshot: ExecutionEnvironmentSnapshot) -> None:
        existing = self._sticky.get(snapshot.execution_identity)
        if existing is not None and existing.snapshot_digest != snapshot.snapshot_digest:
            raise PolicyViolation("Execution identity icin farkli sticky snapshot zaten yuklu")
        self._sticky[snapshot.execution_identity] = snapshot

    def force_probe(
        self, execution_identity: str, *, now: dt.datetime
    ) -> tuple[ExecutionEnvironmentSnapshot, EnvironmentDriftReport]:
        sticky = self.sticky(execution_identity)
        current = self.probe_adapter.probe(execution_identity, force=True)
        self.repository.create_environment_snapshot(current)
        report = detect_environment_drift(sticky, current, checked_at=now)
        self.repository.record_environment_probe(report)
        return current, report

    def assert_dispatch_current(
        self, execution_identity: str, *, now: dt.datetime
    ) -> ExecutionEnvironmentSnapshot:
        sticky = self.sticky(execution_identity)
        if sticky.expires_at <= now:
            raise PolicyViolation("Sticky execution environment snapshot expired")
        _current, report = self.force_probe(execution_identity, now=now)
        report.assert_current()
        return sticky


@dataclass(frozen=True, slots=True)
class BoundEnvironmentEffectGuard:
    """Production effect boundary: persisted sticky state + mandatory live force probe."""

    probe_adapter: EnvironmentProbe
    repository: EnvironmentSnapshotStore

    def assert_envelope_current(
        self, envelope_id: UUID, *, now: dt.datetime
    ) -> ExecutionEnvironmentSnapshot:
        sticky = self.repository.environment_for_envelope(envelope_id)
        service = EnvironmentSnapshotService(self.probe_adapter, self.repository)
        service.attach_sticky(sticky)
        return service.assert_dispatch_current(sticky.execution_identity, now=now)
