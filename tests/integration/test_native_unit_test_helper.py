"""Native yardimci olcum yolu: gercek Maven/JaCoCo sureci + gercek SQLite ledger (A25, A26, A27).

OpenCode executable'i, model veya provider yoktur. Testler sentetik Maven fixture'ini gecici
bir git deposuna kopyalar, gercek ``mvn`` surecini calistirir ve ledger/CAS kanitini okur.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from tests.integration.native_unit_test_support import (
    EXTRA_TEST,
    EXTRA_TEST_BODY,
    SOURCE,
    Env,
    make_env,
)

from zekam.application.unit_test_loop_state import MeasuredRun
from zekam.application.unit_test_runtime import NativeOutcome
from zekam.domain.unit_test_engineering import AttemptState
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("mvn") is None, reason="Maven bu cihazda kurulu degil"),
]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


def test_native_measure_without_opencode_records_real_receipt(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A25: OpenCode yokken gercek olcum + kayit; hicbir model/adapter baslamaz."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("native yol OpenCode adapter'i kuramaz")

    monkeypatch.setattr("zekam.application.unit_test_runtime.opencode_adapter", forbidden)
    request = env.request(percent="50")
    result = env.measure(request)
    assert result.outcome is NativeOutcome.TARGET_MET, result.to_document()
    assert result.target_met and result.process_exit_code == 0
    document = result.to_document()
    assert document["model_calls"] == 0 and document["opencode_required"] is False
    assert document["terminal_recorded"] is False  # olcum != terminal/Work kapanisi
    assert dict(result.test_counts)["executed"] == 2
    assert result.freshness == "fresh" and result.drift == ()
    attempts, receipts, observations, terminals = env.ledger_state(request)
    assert len(attempts) == 1 and terminals == ()
    receipt = receipts[attempts[0].attempt_id]
    assert receipt is not None and receipt.status is AttemptState.COMPLETED
    assert receipt.evidence_digest == result.evidence_digest
    measured = observations[attempts[0].attempt_id]
    assert measured and all(item.covered is not None for item in measured)
    evidence = env.evidence(receipt.evidence_digest)
    assert evidence["contract"] == "zekam-unit-test-native-evidence/v1"
    inner = evidence["evidence"]
    assert inner["outcome"] == "target-met"
    assert inner["measurement"]["exit_code"] == 0
    assert inner["environment_before"]["source_revision"] == env.revision
    assert inner["authority"] == "observation-only"


def test_controlled_remeasure_below_target_then_met_then_budget_stops(env: Env) -> None:
    """Kontrollu tekrar olcum: eksik analiz -> test ekle -> yeniden olc; butce sert durur."""

    request = env.request(percent="80", max_attempts=2)
    first = env.measure(request)
    assert first.outcome is NativeOutcome.BELOW_TARGET, first.to_document()
    assert first.process_exit_code == 22 and not first.target_met
    assert first.remaining_attempts == 1
    assert "testler ekleyin" in first.next_safe_action

    (env.root / EXTRA_TEST).write_text(EXTRA_TEST_BODY, encoding="utf-8")
    second = env.measure(request)
    assert second.outcome is NativeOutcome.TARGET_MET, second.to_document()
    assert second.ordinal == 2 and second.remaining_attempts == 0
    attempts, receipts, _, _ = env.ledger_state(request)
    assert [a.ordinal for a in attempts] == [1, 2]
    assert attempts[1].parent_attempt_id == attempts[0].attempt_id
    # Aday digest'i test agaci icerigini yansitir: ikinci olcum farkli test icerigiyle yapildi.
    assert attempts[0].candidate_digest != attempts[1].candidate_digest
    assert all(r is not None for r in receipts.values())

    third = env.measure(request)
    assert third.outcome is NativeOutcome.BUDGET_EXHAUSTED
    assert third.attempt_id is None and third.process_exit_code == 20
    assert len(env.ledger_state(request)[0]) == 2  # butce asildi: yeni claim yok


def test_no_tests_with_stale_full_coverage_report_is_not_success(env: Env) -> None:
    """A27: eski %100 JaCoCo raporu ve test yoklugu coverage/basari uretmez."""

    shutil.rmtree(env.root / "src" / "test")
    stale = env.root / "target" / "site" / "jacoco"
    stale.mkdir(parents=True)
    (stale / "jacoco.xml").write_text(
        '<?xml version="1.0"?><report name="stale"><package name="com/zekam/fixture">'
        '<class name="com/zekam/fixture/Calculator" sourcefilename="Calculator.java">'
        '<counter type="LINE" missed="0" covered="9"/></class>'
        '<sourcefile name="Calculator.java"><counter type="LINE" missed="0" covered="9"/>'
        "</sourcefile></package></report>",
        encoding="utf-8",
    )
    old = time.time() - 86400
    os.utime(stale / "jacoco.xml", (old, old))
    request = env.request(percent="10")
    result = env.measure(request)
    assert not result.target_met
    assert result.outcome in {NativeOutcome.NO_TESTS, NativeOutcome.STALE_OR_UNBOUND}, (
        result.to_document()
    )
    assert result.observations == () or result.outcome is not NativeOutcome.TARGET_MET
    _, receipts, observations, _ = env.ledger_state(request)
    (attempt_id,) = receipts
    assert receipts[attempt_id].status is AttemptState.FAILED
    assert observations[attempt_id] == ()  # basarisiz olcum kapsama gozlemi tasimaz
    assert "yazin" in result.next_safe_action or "taze" in result.next_safe_action


def test_failing_test_is_reported_with_cases_and_no_coverage(env: Env) -> None:
    test_file = env.root / "src/test/java/com/zekam/fixture/CalculatorTest.java"
    test_file.write_text(
        test_file.read_text(encoding="utf-8").replace("assertEquals(5,", "assertEquals(6,"),
        encoding="utf-8",
    )
    request = env.request(percent="10")
    result = env.measure(request)
    assert result.outcome is NativeOutcome.TESTS_FAILED, result.to_document()
    assert any("addsValues" in case for case in result.failed_cases)
    assert not result.target_met and result.process_exit_code == 24
    _, receipts, observations, _ = env.ledger_state(request)
    (attempt_id,) = receipts
    assert receipts[attempt_id].status is AttemptState.FAILED
    assert observations[attempt_id] == ()


def test_source_drift_during_measurement_rejects_coverage(env: Env) -> None:
    """Olcum sirasinda production degisirse gercek surec basarili olsa bile kabul yok."""

    production = env.root / SOURCE

    class DriftingMeasurer:
        def __init__(self, inner: object) -> None:
            self.inner = inner

        def measure(self, *args: object, **kwargs: object) -> MeasuredRun:
            run = self.inner.measure(*args, **kwargs)  # type: ignore[attr-defined]
            production.write_text(
                production.read_text(encoding="utf-8") + "\n// drift\n", encoding="utf-8"
            )
            return run

    request = env.request(percent="10")
    result = env.measure(request, wrap=DriftingMeasurer)
    assert result.outcome is NativeOutcome.SOURCE_DRIFT, result.to_document()
    assert "production" in result.drift
    assert not result.target_met and result.process_exit_code == 25
    _, receipts, observations, _ = env.ledger_state(request)
    (attempt_id,) = receipts
    assert receipts[attempt_id].status is AttemptState.FAILED
    assert observations[attempt_id] == ()


def test_test_tree_change_during_measurement_is_drift(env: Env) -> None:
    class TestWriter:
        def __init__(self, inner: object) -> None:
            self.inner = inner

        def measure(self, *args: object, **kwargs: object) -> MeasuredRun:
            run = self.inner.measure(*args, **kwargs)  # type: ignore[attr-defined]
            (env.root / EXTRA_TEST).write_text(EXTRA_TEST_BODY, encoding="utf-8")
            return run

    result = env.measure(env.request(percent="10"), wrap=TestWriter)
    assert result.outcome is NativeOutcome.SOURCE_DRIFT
    assert "test-tree-during-measurement" in result.drift
    assert not result.target_met


def test_wrong_target_and_revision_mismatch_claim_nothing(env: Env) -> None:
    wrong = env.request(source="src/main/java/com/zekam/fixture/Missing.java")
    result = env.measure(wrong)
    assert result.outcome is NativeOutcome.WRONG_TARGET, result.to_document()
    assert result.attempt_id is None and result.process_exit_code == 27
    assert env.ledger_state(wrong)[0] == ()

    stale_revision = env.request(revision="0" * 40)
    result = env.measure(stale_revision)
    assert result.outcome is NativeOutcome.SOURCE_DRIFT and result.attempt_id is None
    assert env.ledger_state(stale_revision)[0] == ()


def test_process_timeout_is_incomplete_and_interrupted_not_success(env: Env) -> None:
    request = env.request(percent="10", timeout=1)
    result = env.measure(request)
    assert not result.target_met
    assert result.outcome is NativeOutcome.INCOMPLETE, result.to_document()
    _, receipts, observations, _ = env.ledger_state(request)
    (attempt_id,) = receipts
    assert receipts[attempt_id].status is AttemptState.INTERRUPTED
    assert observations[attempt_id] == ()


def test_orphan_claim_is_recovered_as_interrupted_then_measurement_runs(env: Env) -> None:
    """Cokmus (receipt'siz) onceki claim INTERRUPTED kapatilir; isi tekrar calistirilmaz."""

    import datetime as dt

    from zekam.domain.unit_test_engineering import UnitTestAttempt

    request = env.request()
    # Gercek bir olcum cokusunu taklit etmeden: ledger'a claim yazilir, receipt yazilmaz.
    store = SQLiteOperationalStore(env.database)
    with store.unit_of_work() as uow:
        ledger = uow.unit_test_ledger()
        ledger.register_request(request, now=dt.datetime.now(dt.UTC))
        orphan = UnitTestAttempt(
            attempt_id="ut-orphan-n1",
            request_digest=request.request_digest,
            ordinal=1,
            parent_attempt_id=None,
            plan_digest=request.request_digest,
            candidate_digest=request.request_digest,
            idempotency_key="ut-orphan-n1-claim",
        )
        ledger.claim_attempt(orphan, now=dt.datetime.now(dt.UTC))
        assert ledger.unreceipted_attempts(request.request_digest) == ("ut-orphan-n1",)
        uow.commit()

    result = env.measure(request)
    assert result.outcome is NativeOutcome.TARGET_MET  # kurtarma sonrasi gercek olcum surer
    attempts, receipts, _, _ = env.ledger_state(request)
    assert [a.ordinal for a in attempts] == [1, 2]
    assert receipts["ut-orphan-n1"].status is AttemptState.INTERRUPTED
    assert result.detail == ("ut-orphan-n1",)
