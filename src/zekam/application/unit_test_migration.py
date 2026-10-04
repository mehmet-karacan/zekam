"""Explicit local admission for the operational unit-test ledger migration."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from zekam.application.composition import ApplicationContext
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.local_improvement import SQLiteLocalImprovementStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore


class UnitTestLedgerMigrationAdmission:
    """Fence local writers before the additive v5-to-v6 ledger migration.

    The migration is deliberately narrower than the evolution bootstrap path: it
    requires the existing evolution control plane to be paused or disabled and
    never grants a standing evolution authority. The explicit CLI plan digest is
    the outer owner admission for this one-shot local schema effect.
    """

    def __init__(self, context: ApplicationContext) -> None:
        database = context.settings.database.sqlite_path(context.home)
        self._runtime = SQLiteLocalRuntimeStore(database, existing_only=True)
        self._control = SQLiteLocalImprovementStore(
            context.home / "state" / "improvement.db",
            context.home / "state" / "learning.db",
            context.home / "benchmarklar" / "benchmark.db",
        )
        self._home = context.home

    def trusted_home(self) -> Path:
        return self._home

    def stop_new_admission(self) -> None:
        control = self._control.evolution_control_status()
        if control.get("state") not in {"paused", "disabled"}:
            raise PolicyViolation(
                "Unit-test ledger migration requires paused or disabled evolution control"
            )

    def drain_and_reap(self) -> None:
        self._runtime.recover_expired()
        self._runtime.recover_outbox()

    def assert_no_admitted_authority(self) -> None:
        control = self._control.evolution_control_status()
        if control.get("state") not in {"paused", "disabled"}:
            raise PolicyViolation("Unit-test ledger migration admission is not paused")
        runtime = self._runtime.status()
        if any(
            (
                runtime.running_jobs,
                runtime.recovery_jobs,
                runtime.claimed_outbox,
                runtime.recovery_outbox,
                runtime.open_recovery_cases,
            )
        ):
            raise PolicyViolation("Unit-test ledger migration runtime is not quiescent")

    def release_admission(self) -> None:
        return

    def mark_recovery_required(self) -> None:
        control = self._control.evolution_control_status()
        if control.get("state") != "disabled":
            self._control.set_evolution_control_state(
                "disabled",
                reason="operational-v6-recovery",
                now=dt.datetime.now(dt.UTC),
            )
