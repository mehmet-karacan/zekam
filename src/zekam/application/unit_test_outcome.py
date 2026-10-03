"""`zekam test` makine-okunur sonuc taksonomisi ve cikis kodlari (W07, AKTIF_GOREV 9.3/10.2).

``success=true`` yoktur. Her sonuc ``status`` (mevcut ``LoopTerminalState``), ``reason``
(``UnitTestStopReason`` ya da kayit-oncesi kapi nedeni), ``final`` ve cikis kodunu birlikte
tasir. Basari yalniz ledger'da receipt'li ``target-reached`` / ``already-at-target`` terminalinden
gelir; bu modul yetki uretmez, yalniz kaydi anlamlandirir.

Cikis kodlari (kararli; otomasyon ``0`` ve ``10`` degerlerini "hedef karsilandi" sayabilir):

====  ===============================  ========================================================
kod   reason / kapi                    anlam
====  ===============================  ========================================================
0     target-reached                   hedef + zorunlu kalite/final dogrulama gecti (receipt var)
10    already-at-target                baslangic olcumunde zaten hedefte (degisiklik yok)
20    budget-exhausted                 butce bitti (refactor zorunlulugu degildir)
21    stagnation-review                plateau: ek ilerleme bulunamadi, insan incelemesi
30    environment-missing              ortam/arac/izin/migration/ajan koprusu eksik
31    technology-unsupported           teknoloji desteklenmiyor (Maven disi)
32    spec-ambiguous                   beklenen davranis belirsiz (oracle yok)
33    measurement-incomplete           olcum eksik/gecersiz; basari denmez
40    production-defect                gercek production hatasi bulundu; reproducer korunur
41    refactor-approval-required       ayri testability refactor onayi gerekir
50    user-paused                      kullanici durdurdu; ``resume`` ile devam edilebilir
51    user-cancelled                   kullanici iptal etti (final)
52    recovery-required                kesinti; ``resume`` ile kurtarma gerekir
60    already-running                  ayni istegi baska bir surec calistiriyor
64    usage-error                      gecersiz/eksik arguman
65    clarification-required           hedef/esik belirsiz; once netlestirme
66    not-found                        kayit/proje/istek bulunamadi
70    runtime-error                    beklenmeyen calisma hatasi
77    policy-violation                 exact plan digest/yetki/butce kapisi reddetti
====  ===============================  ========================================================
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from zekam.domain.unit_test_engineering import (
    RESUMABLE_REASONS,
    UnitTestStopReason,
    UnitTestTerminal,
)

EXIT_TARGET_REACHED: Final = 0
EXIT_ALREADY_AT_TARGET: Final = 10
EXIT_BUDGET_EXHAUSTED: Final = 20
EXIT_STAGNATION: Final = 21
EXIT_ENVIRONMENT_MISSING: Final = 30
EXIT_TECHNOLOGY_UNSUPPORTED: Final = 31
EXIT_SPEC_AMBIGUOUS: Final = 32
EXIT_MEASUREMENT_INCOMPLETE: Final = 33
EXIT_PRODUCTION_DEFECT: Final = 40
EXIT_REFACTOR_APPROVAL: Final = 41
EXIT_USER_PAUSED: Final = 50
EXIT_USER_CANCELLED: Final = 51
EXIT_RECOVERY_REQUIRED: Final = 52
EXIT_ALREADY_RUNNING: Final = 60
EXIT_USAGE: Final = 64
EXIT_CLARIFICATION: Final = 65
EXIT_NOT_FOUND: Final = 66
EXIT_RUNTIME: Final = 70
EXIT_POLICY: Final = 77

REASON_EXIT_CODES: Final[Mapping[UnitTestStopReason, int]] = {
    UnitTestStopReason.TARGET_REACHED: EXIT_TARGET_REACHED,
    UnitTestStopReason.ALREADY_AT_TARGET: EXIT_ALREADY_AT_TARGET,
    UnitTestStopReason.BUDGET_EXHAUSTED: EXIT_BUDGET_EXHAUSTED,
    UnitTestStopReason.STAGNATION_REVIEW: EXIT_STAGNATION,
    UnitTestStopReason.ENVIRONMENT_MISSING: EXIT_ENVIRONMENT_MISSING,
    UnitTestStopReason.TECHNOLOGY_UNSUPPORTED: EXIT_TECHNOLOGY_UNSUPPORTED,
    UnitTestStopReason.SPEC_AMBIGUOUS: EXIT_SPEC_AMBIGUOUS,
    UnitTestStopReason.MEASUREMENT_INCOMPLETE: EXIT_MEASUREMENT_INCOMPLETE,
    UnitTestStopReason.PRODUCTION_DEFECT: EXIT_PRODUCTION_DEFECT,
    UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED: EXIT_REFACTOR_APPROVAL,
    UnitTestStopReason.USER_PAUSED: EXIT_USER_PAUSED,
    UnitTestStopReason.USER_CANCELLED: EXIT_USER_CANCELLED,
    UnitTestStopReason.RECOVERY_REQUIRED: EXIT_RECOVERY_REQUIRED,
}
if set(REASON_EXIT_CODES) != set(UnitTestStopReason):  # pragma: no cover - import-time guard
    raise RuntimeError("Her stop reason bir cikis koduna eslenmeli")

#: Kayit-oncesi kapi sonuclari (ledger'a terminal yazilmaz; ``recorded=false``).
GATE_EXIT_CODES: Final[Mapping[str, int]] = {
    "ready": 0,
    "usage-error": EXIT_USAGE,
    "clarification-required": EXIT_CLARIFICATION,
    "tool-missing": EXIT_ENVIRONMENT_MISSING,
    "setup-required": EXIT_ENVIRONMENT_MISSING,
    "environment-missing": EXIT_ENVIRONMENT_MISSING,
    "not-supported": EXIT_TECHNOLOGY_UNSUPPORTED,
    "already-running": EXIT_ALREADY_RUNNING,
    "policy-violation": EXIT_POLICY,
    "not-found": EXIT_NOT_FOUND,
}

#: ``resume`` yalniz bu durumlari devam ettirir; final terminal yeniden calistirilmaz.
RESUMABLE: Final = frozenset(reason.value for reason in RESUMABLE_REASONS)


def exit_code_for_reason(reason: UnitTestStopReason) -> int:
    return REASON_EXIT_CODES[reason]


def terminal_document(terminal: UnitTestTerminal, *, recorded: bool = True) -> dict[str, Any]:
    """Ledger terminalinin makine-okunur karsiligi; ``success`` alani yoktur."""

    reason = terminal.stop_reason
    return {
        "status": terminal.status.value,
        "reason": reason.value,
        "final": terminal.final,
        "resumable": reason.value in RESUMABLE,
        "exit_code": exit_code_for_reason(reason),
        "receipt_recorded": terminal.receipt_digest is not None,
        "receipt_digest": terminal.receipt_digest,
        "evidence_digest": terminal.evidence_digest,
        "recorded": recorded,
    }


def gate_document(
    gate: str, *, reasons: tuple[str, ...] = (), next_steps: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Kayit-oncesi kapi (ornegin ``tool-missing``): hicbir effect baslamadi."""

    code = GATE_EXIT_CODES.get(gate, EXIT_RUNTIME)
    return {
        "status": "blocked" if code not in {0} else "ready",
        "reason": gate,
        "final": False,
        "resumable": False,
        "exit_code": code,
        "receipt_recorded": False,
        "recorded": False,
        "details": list(reasons),
        "next_steps": list(next_steps),
    }


def documented_exit_codes() -> dict[str, int]:
    """Belge/test icin kararli tablo (reason/kapi -> kod)."""

    table: dict[str, int] = {reason.value: code for reason, code in REASON_EXIT_CODES.items()}
    table.update(
        {
            "already-running": EXIT_ALREADY_RUNNING,
            "usage-error": EXIT_USAGE,
            "clarification-required": EXIT_CLARIFICATION,
            "not-found": EXIT_NOT_FOUND,
            "runtime-error": EXIT_RUNTIME,
            "policy-violation": EXIT_POLICY,
        }
    )
    return table
