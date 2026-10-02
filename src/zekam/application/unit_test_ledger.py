"""Unit-test mühendisligi kalici ledger portu.

Application servisleri SQLite/PostgreSQL adaptorune degil bu porta baglanir.
Port mevcut operational unit-of-work sinirini degistirmez: adaptor, cagiranin
actigi (ve commit/rollback'ini yonettigi) operational transaction baglantisi
uzerinde calisir; kendi ayri authority veritabani yoktur.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from zekam.domain.unit_test_engineering import (
    AttemptState,
    CoverageObservation,
    UnitTestAttempt,
    UnitTestRequest,
    UnitTestTerminal,
)


@dataclass(frozen=True, slots=True)
class LedgerWrite:
    """Yazma sonucu; ``replayed`` ayni islemin ikinci kez uygulanmadigini gosterir."""

    key: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class AttemptReceiptRecord:
    attempt_id: str
    status: AttemptState
    evidence_digest: str


class UnitTestLedger(Protocol):
    def register_request(self, request: UnitTestRequest, *, now: datetime) -> LedgerWrite: ...

    def claim_attempt(self, attempt: UnitTestAttempt, *, now: datetime) -> LedgerWrite: ...

    def record_attempt_receipt(
        self,
        *,
        attempt_id: str,
        status: AttemptState,
        evidence_digest: str,
        observations: tuple[CoverageObservation, ...],
        now: datetime,
    ) -> LedgerWrite: ...

    def unreceipted_attempts(self, request_digest: str) -> tuple[str, ...]: ...

    def list_attempts(self, request_digest: str) -> tuple[UnitTestAttempt, ...]: ...

    def get_attempt_receipt(self, attempt_id: str) -> AttemptReceiptRecord | None: ...

    def list_observations(self, attempt_id: str) -> tuple[CoverageObservation, ...]: ...

    def record_terminal(self, terminal: UnitTestTerminal, *, now: datetime) -> LedgerWrite: ...

    def list_terminals(self, request_digest: str) -> tuple[UnitTestTerminal, ...]: ...


class UnitTestLedgerUnitOfWork(Protocol):
    """Operational unit-of-work uzantisi; mevcut Protocol'u degistirmez."""

    def unit_test_ledger(self) -> UnitTestLedger: ...
