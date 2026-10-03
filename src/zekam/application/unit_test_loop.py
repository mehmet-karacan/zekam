"""Agentic unit-test dongusu servisi (W05, AKTIF_GOREV 9.1).

Omurga: scope -> readiness/baseline -> davranis/gap plani -> onayli test-only attempt ->
gercek calistirma/olcum -> bagimsiz kabul -> kalan gap/repair/replan -> final dogrulama.

Ilkeler:

- Claim-before-effect: aday patch'in manifesti/preimage'lari nesne deposuna yazilir, SONRA
  attempt ledger'da claim edilir, SONRA patch uygulanir ve olcum kosulur. Receipt'siz
  basari yoktur; terminal basari ledger'in tam hassasiyetli yeniden degerlendirmesinden gecer.
- Olcum/kabul kararini harness verir (``unit_test_measurement``); model sayac/basari beyan
  edemez. Builder kendi degisikliginin verifier'i olamaz: ayri assignment/invocation/agent_id.
- Test-only: production, POM, coverage ayari, verifier varliklari ve kullanici dirty
  dosyalari korunur. Reddedilen adayda yalniz gorevin kendi degisikligi preimage/ownership
  kontroluyle geri alinir.
- Bug bulan testin beklentisi bozuk production ciktisina cevrilmez: reproducer + oracle
  korunur (frozen), defect proposal uretilir, basari yazilmaz.
- Cancel/timeout sonrasi yeni attempt baslamaz; patch sahipligi readback ile kanita yazilir.
  Resume tamamlanmis attempt'i yeniden calistirmaz; kabul edilmis kendi test patch'i drift
  degildir, kaynak/config/plan degisimi eski basariyi tasimaz.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from zekam.application.object_store import ObjectStore
from zekam.application.unit_test_agents import (
    VERIFYING,
    AgentDispatchFailure,
    AgentResponse,
    AgentSpecialty,
    AgentTask,
    BuildBody,
    UnitTestAgentGateway,
    Verdict,
    parse_build_body,
    parse_plan_body,
    parse_verify_body,
)
from zekam.application.unit_test_failure import (
    DefectProposal,
    FailureClassification,
    FailureKind,
    FailureSignals,
    FlakyDiagnosis,
    RepeatRun,
    classify_failure,
)
from zekam.application.unit_test_ledger import UnitTestLedger
from zekam.application.unit_test_loop_state import (
    ControlSignal,
    EnvironmentObservation,
    EnvironmentObserver,
    InMemoryLoopControl,
    LoopControl,
    LoopOutcome,
    LoopState,
    MeasuredRun,
    UnitTestMeasurer,
)
from zekam.application.unit_test_measurement import (
    CurrentState,
    FreshnessVerdict,
    MeasurementRecord,
    MeasurementStatus,
    check_freshness,
)
from zekam.application.unit_test_patch import (
    AppliedFile,
    AppliedPatch,
    CandidateManifest,
    GuardViolation,
    PreimageMismatch,
    RevertReceipt,
    TestOnlyPolicy,
    TestPatchWorkspace,
    accepted_state_digest,
    find_smell_candidates,
    has_executable_test,
)
from zekam.application.unit_test_plan import (
    BehaviorPlan,
    OracleKind,
    ScenarioBoard,
    ScenarioState,
    actionable_scenarios,
    parse_behavior_plan,
    scenarios_in_state,
    unresolved_material,
)
from zekam.application.unit_test_plateau import (
    AttemptOutcomeKind,
    AttemptRecord,
    LoopLimits,
    PlateauActionKind,
    PlateauPolicy,
    ProgressGain,
    ProgressKind,
    StrategyKind,
    UnitTestApprovals,
    approach_digest,
    detect_plateau,
    plan_plateau_response,
    state_gap_digest,
)
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import (
    AttemptState,
    EvaluationVerdict,
    UnitTestAttempt,
    UnitTestRequest,
    UnitTestStopReason,
    UnitTestTerminal,
)

#: Yeniden olcum gerektiren harici drift nedenleri (source-revision ayri: yeni istek ister).
_REBASELINE_DRIFT = frozenset({"toolchain", "config", "production", "test-tree"})


def _hex16(value: str) -> str:
    return value.split(":", 1)[-1][:16]


@dataclass(frozen=True, slots=True)
class UnitTestLoopService:
    """Port'lara bagli dongu; SQLite/PostgreSQL/Maven/model somut tiplerini bilmez."""

    ledger: UnitTestLedger
    objects: ObjectStore
    gateway: UnitTestAgentGateway
    measurer: UnitTestMeasurer
    workspace: TestPatchWorkspace
    environment: EnvironmentObserver
    control: LoopControl | None = None
    plateau_policy: PlateauPolicy = field(default_factory=PlateauPolicy)
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    monotonic: Callable[[], float] = time.monotonic

    def run(
        self,
        request: UnitTestRequest,
        *,
        approvals: UnitTestApprovals,
        limits: LoopLimits | None = None,
    ) -> LoopOutcome:
        limits = limits or LoopLimits()
        control = self.control or InMemoryLoopControl()
        return _Session(self, request, approvals, limits, control).execute()


def _reproducer_paths(
    failed_cases: tuple[str, ...], patch_paths: tuple[str, ...]
) -> tuple[str, ...]:
    """Basarisiz test sinifinin dosyalari; eslesme yoksa muhafazakar olarak tum patch."""

    classes = {case.split("#")[0].rsplit(".", 1)[-1] for case in failed_cases}
    matched = tuple(p for p in patch_paths if p.rsplit("/", 1)[-1].removesuffix(".java") in classes)
    return matched or patch_paths


def _scenarios_for_path(path: str, links: Any) -> list[str]:
    stem = path.rsplit("/", 1)[-1].removesuffix(".java")
    matched = sorted(
        {k.scenario_id for k in links if k.test_ref.split("#")[0].rsplit(".", 1)[-1] == stem}
    )
    return matched or sorted({link.scenario_id for link in links})


class _Stop(Exception):
    """Dahili: dongu terminal sonuca ulasti."""

    def __init__(self, outcome: LoopOutcome) -> None:
        super().__init__("stop")
        self.outcome = outcome


class _Session:
    def __init__(
        self,
        service: UnitTestLoopService,
        request: UnitTestRequest,
        approvals: UnitTestApprovals,
        limits: LoopLimits,
        control: LoopControl,
    ) -> None:
        self.s = service
        self.request = request
        self.approvals = approvals
        self.limits = limits
        self.control = control
        self.rd = request.request_digest
        self.state = LoopState()
        self.executed = 0
        self._tick_at = service.monotonic()

    # -- yardimcilar --------------------------------------------------------------------

    def _now(self) -> dt.datetime:
        return self.s.clock()

    def _tick(self) -> None:
        now = self.s.monotonic()
        self.state.meter.elapsed_seconds += max(0.0, now - self._tick_at)
        self._tick_at = now

    def _attempts(self) -> int:
        return len(self.s.ledger.list_attempts(self.rd))

    def _put(self, payload: Mapping[str, Any], media_type: str = "application/json") -> str:
        return self.s.objects.put(canonical_bytes(payload), media_type=media_type).digest

    def _outcome(self, terminal: UnitTestTerminal, detail: tuple[str, ...]) -> LoopOutcome:
        state = self.state
        needs_spec: tuple[str, ...] = ()
        if state.plan is not None:
            needs_spec = tuple(
                s.scenario_id
                for s in scenarios_in_state(
                    state.plan, state.board, ScenarioState.NEEDS_SPECIFICATION
                )
            )
        return LoopOutcome(
            self.rd,
            terminal,
            self._attempts(),
            self.executed,
            detail,
            tuple(state.defects),
            needs_spec,
            tuple(state.testability_findings),
        )

    def _stop(
        self,
        reason: UnitTestStopReason,
        detail: tuple[str, ...] = (),
        *,
        receipt_digest: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> _Stop:
        self._tick()
        evidence = self._put(
            {
                "contract": "zekam-unit-test-terminal-evidence/v1",
                "request_digest": self.rd,
                "stop_reason": reason.value,
                "detail": list(detail),
                "state": self.state.to_payload(),
                "extra": dict(extra or {}),
            }
        )
        terminal = UnitTestTerminal(self.rd, reason, receipt_digest, evidence)
        self.s.ledger.record_terminal(terminal, now=self._now())
        return _Stop(self._outcome(terminal, detail))

    # -- giris ---------------------------------------------------------------------------

    def execute(self) -> LoopOutcome:
        self.s.ledger.register_request(self.request, now=self._now())
        terminals = self.s.ledger.list_terminals(self.rd)
        if terminals and terminals[-1].final:
            return self._outcome(terminals[-1], ("already-final",))
        try:
            self._admit()
            self._recover()
            return self._loop()
        except _Stop as stop:
            return stop.outcome

    def _admit(self) -> None:
        missing = self.approvals.missing_for_run(remote_agents=self.s.gateway.remote)
        if missing:
            raise self._stop(
                UnitTestStopReason.ENVIRONMENT_MISSING,
                tuple(f"permission-missing:{name}" for name in missing),
            )
        if (
            self.s.gateway.remote or self.approvals.remote_model
        ) and self.limits.max_remote_calls < 1:
            # B3: 0 "sinirsiz" degil, "remote cagri yok" demektir; remote icin acik butce zorunlu.
            raise self._stop(
                UnitTestStopReason.ENVIRONMENT_MISSING, ("remote-call-budget-missing",)
            )
        if self.approvals.budget_increase_needs_approval(self.request.budget, self.limits):
            raise self._stop(
                UnitTestStopReason.ENVIRONMENT_MISSING, ("budget-increase-needs-approval",)
            )

    def _load_evidence(self, evidence_digest: str) -> dict[str, Any]:
        loaded = json.loads(self.s.objects.get(evidence_digest).decode("utf-8"))
        if not isinstance(loaded, dict):
            raise ValidationFailed("Kanit nesnesi sozluk degil")
        return loaded

    def _recover(self) -> None:
        """Ledger'dan resume: son receipt'li kanit durumu; receipt'siz claim kapatilir."""

        attempts = self.s.ledger.list_attempts(self.rd)
        previous: dict[str, Any] | None = None
        for attempt in attempts:
            receipt = self.s.ledger.get_attempt_receipt(attempt.attempt_id)
            if receipt is not None:
                previous = self._load_evidence(receipt.evidence_digest)
        if previous is not None:
            self.state = LoopState.from_payload(previous["state"], self.request)
        for attempt_id in self.s.ledger.unreceipted_attempts(self.rd):
            attempt = next(a for a in attempts if a.attempt_id == attempt_id)
            notes = self._recover_claim(attempt)
            self.state.meter.extra["recovered_attempts"] = (
                self.state.meter.extra.get("recovered_attempts", 0) + 1
            )
            self._close(attempt, AttemptState.INTERRUPTED, {"recovered": notes}, ())

    def _recover_claim(self, attempt: UnitTestAttempt) -> dict[str, Any]:
        """Claim sonrasi coken attempt: ISI yeniden calistirilmaz; kendi patch'i geri alinir."""

        try:
            payload = json.loads(self.s.objects.get(attempt.candidate_digest).decode("utf-8"))
            manifest = CandidateManifest.from_payload(payload)
        except Exception:
            return {"manifest": "unreadable"}
        applied = AppliedPatch(
            tuple(
                AppliedFile(f.path, f.preimage_digest, f.postimage_digest) for f in manifest.files
            )
        )
        receipt = self._revert(applied, manifest)
        return {
            "restored": list(receipt.restored),
            "foreign_modified": list(receipt.foreign_modified),
            "failed": list(receipt.failed),
        }

    # -- cancel/pause kapisi (B2) ve success oncesi taze dogrulama (B1) ------------------

    def _gate(self, *, where: str) -> None:
        """Yeni claim/patch/success oncesi: cancel ve pause kazanir."""

        signal = self.control.signal()
        if signal is ControlSignal.CANCEL:
            raise self._stop(UnitTestStopReason.USER_CANCELLED, (f"cancel-before-{where}",))
        if signal is ControlSignal.PAUSE:
            raise self._stop(UnitTestStopReason.USER_PAUSED, (f"pause-before-{where}",))

    def _stale_reasons(
        self,
        record: MeasurementRecord,
        expected_files: Mapping[str, str],
        expected_candidate: str | None,
    ) -> tuple[str, ...]:
        """Diski YENIDEN okuyup kayitli digest'lerle karsilastirir.

        M13: kendi test yollari (``expected_files``) drift sayilmaz ama digest'leri diskle
        BIREBIR eslesmelidir; M12: bunlarin disindaki her test/production/config/toolchain
        degisimi harici drift'tir. Aday kimligi olcumun bagli oldugu degerle degil
        beklenen (harness'in kendi hesapladigi) degerle karsilastirilir.
        """

        reasons: list[str] = []
        observation = self.s.environment.observe()
        verdict = self._current_for(observation, record, expected_candidate)
        if verdict is not FreshnessVerdict.FRESH:
            reasons.append(f"freshness:{verdict.value}")
        on_disk = self.s.workspace.digest_paths(expected_files)
        for path, recorded in sorted(expected_files.items()):
            if on_disk.get(path) != recorded:
                reasons.append(f"test-patch-modified:{path}")
        baseline = self.state.baseline_obs
        if baseline is not None:
            drift = observation.drifted_from(baseline, own_test_paths=frozenset(expected_files))
            reasons += [f"drift:{item}" for item in drift]
        accepted_plan = self.state.accepted_plan_digest
        if (
            accepted_plan is not None
            and self.state.plan is not None
            and self.state.plan.plan_digest != accepted_plan
        ):
            reasons.append("plan-changed")
        return tuple(reasons)

    def _mark_stale(self, reasons: tuple[str, ...]) -> None:
        """Eski basari/dogrulama tasinmaz (M12/M13): sahipligi bozulan dosyalar birakilir."""

        tampered = tuple(
            r.removeprefix("test-patch-modified:")
            for r in reasons
            if r.startswith("test-patch-modified:")
        )
        self.state.stale_flag = True
        self._invalidate_for_drift(tampered)

    def _succeed(
        self,
        reason: UnitTestStopReason,
        detail: tuple[str, ...],
        *,
        record: MeasurementRecord,
        receipt_digest: str,
        expected_files: Mapping[str, str],
        expected_candidate: str | None,
        extra: Mapping[str, Any] | None = None,
    ) -> bool:
        """Basari terminalinden HEMEN once cancel + taze dogrulama; eskiyse basari YAZILMAZ."""

        self._gate(where="success")
        reasons = self._stale_reasons(record, expected_files, expected_candidate)
        if reasons:
            self._mark_stale(reasons)
            self.state.meter.extra["stale_success_refusals"] = (
                self.state.meter.extra.get("stale_success_refusals", 0) + 1
            )
            if self.state.meter.extra["stale_success_refusals"] > self.limits.max_replans + 2:
                raise self._stop(
                    UnitTestStopReason.MEASUREMENT_INCOMPLETE,
                    ("success-stale-repeatedly", *reasons),
                )
            return False
        raise self._stop(reason, detail, receipt_digest=receipt_digest, extra=extra)

    # -- ana dongu -----------------------------------------------------------------------

    def _loop(self) -> LoopOutcome:
        state = self.state
        while True:
            signal = self.control.signal()
            if signal is ControlSignal.CANCEL:
                raise self._stop(UnitTestStopReason.USER_CANCELLED, ("cancel-before-attempt",))
            if signal is ControlSignal.PAUSE:
                raise self._stop(UnitTestStopReason.USER_PAUSED, ("pause-before-attempt",))
            self._tick()
            exceeded = state.meter.exceeded(
                self.request.budget, self.limits, attempts_used=self._attempts()
            )
            if exceeded:
                raise self._stop(UnitTestStopReason.BUDGET_EXHAUSTED, exceeded)
            self._plateau_step()
            if state.baseline_obs is None or state.needs_rebaseline:
                self._measure_only_attempt()
                continue
            observation = self.s.environment.observe()
            drift = observation.drifted_from(
                state.baseline_obs, own_test_paths=frozenset(state.accepted_files)
            )
            if "source-revision" in drift:
                raise self._stop(
                    UnitTestStopReason.MEASUREMENT_INCOMPLETE, ("source-revision-drift",)
                )
            owned_now = self.s.workspace.digest_paths(state.accepted_files)
            tampered = tuple(p for p, d in state.accepted_files.items() if owned_now.get(p) != d)
            if set(drift) & _REBASELINE_DRIFT or tampered:
                self._invalidate_for_drift(tampered)
                continue
            self._build_attempt(observation)

    def _invalidate_for_drift(self, tampered: tuple[str, ...]) -> None:
        """Harici drift: eski dogrulama/basari TASINMAZ (M13); yeniden baseline."""

        state = self.state
        for path in tampered:
            state.accepted_files.pop(path, None)
        if state.plan is not None:
            state.board = state.board.reset_verified(state.plan)
        state.accepted_observations = {}
        state.accepted_measurement_digest = None
        state.needs_rebaseline = True

    def _plateau_step(self) -> None:
        state = self.state
        signal = detect_plateau(tuple(state.history), self.s.plateau_policy)
        if signal is None:
            return
        action = plan_plateau_response(
            signal,
            tuple(state.history),
            applied_remedies=frozenset(state.remedies),
            tried_strategies=frozenset(state.tried_strategies),
            attempts_remaining=self.request.budget.max_attempts - self._attempts(),
            discovery_used=state.meter.discovery_attempts,
            policy=self.s.plateau_policy,
        )
        kind = action.kind
        if action.remedy_key:
            state.remedies.add(action.remedy_key)
        if kind is PlateauActionKind.REMEASURE_FRESH:
            state.needs_rebaseline = True
        elif kind is PlateauActionKind.RECHECK_SCOPE:
            state.needs_rebaseline = True
            state.scope_flag = False
        elif kind is PlateauActionKind.REFRESH_CONTEXT:
            state.context_flag = False
        elif kind is PlateauActionKind.CHANGE_STRATEGY:
            assert action.strategy is not None
            state.forced_strategy = action.strategy
            state.tried_strategies.add(action.strategy)
            if action.strategy is StrategyKind.SETUP_DISCOVERY:
                state.meter.discovery_attempts += 1
            # plateau sayaci sifirlanmaz ama yeni strateji denenene kadar tekrar tetiklenmez
            state.history.append(
                AttemptRecord(
                    len(state.history) + 1,
                    AttemptOutcomeKind.INTERRUPTED,
                    digest({"strategy-switch": action.strategy.value}),
                    signal.gap_digest,
                    (),
                )
            )
        else:
            findings = tuple(state.testability_findings)
            tested_only = len(state.tried_strategies) >= 2
            if findings and tested_only:
                raise self._stop(
                    UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED,
                    ("testability-evidence", action.message),
                )
            raise self._stop(
                UnitTestStopReason.STAGNATION_REVIEW, (action.message, "no-impossibility-claim")
            )

    # -- attempt yasam dongusu ----------------------------------------------------------

    def _claim(self, manifest: CandidateManifest, plan_digest: str) -> UnitTestAttempt:
        attempts = self.s.ledger.list_attempts(self.rd)
        ordinal = len(attempts) + 1
        attempt = UnitTestAttempt(
            attempt_id=f"ut-{_hex16(self.rd)}-{ordinal}",
            request_digest=self.rd,
            ordinal=ordinal,
            parent_attempt_id=attempts[-1].attempt_id if attempts else None,
            plan_digest=plan_digest,
            candidate_digest=manifest.candidate_digest,
            idempotency_key=f"ut-{_hex16(self.rd)}-{ordinal}-claim",
            changed_files=manifest.changed_files,
        )
        try:
            self.s.ledger.claim_attempt(attempt, now=self._now())
        except PolicyViolation as exc:
            raise self._stop(
                UnitTestStopReason.BUDGET_EXHAUSTED, ("claim-refused", str(exc))
            ) from exc
        self.executed += 1
        return attempt

    def _close(
        self,
        attempt: UnitTestAttempt,
        status: AttemptState,
        evidence: Mapping[str, Any],
        observations: tuple[Any, ...],
    ) -> str:
        self._tick()
        digest_value = self._put(
            {
                "contract": "zekam-unit-test-attempt-evidence/v1",
                "request_digest": self.rd,
                "attempt_id": attempt.attempt_id,
                "ordinal": attempt.ordinal,
                "status": status.value,
                "evidence": dict(evidence),
                "state": self.state.to_payload(),
            }
        )
        self.s.ledger.record_attempt_receipt(
            attempt_id=attempt.attempt_id,
            status=status,
            evidence_digest=digest_value,
            observations=observations,
            now=self._now(),
        )
        return digest_value

    def _run_measure(
        self, attempt: UnitTestAttempt, candidate_digest: str, *, suffix: str = ""
    ) -> MeasuredRun:
        budget = self.request.budget
        remaining = budget.total_elapsed_seconds - self.state.meter.elapsed_seconds
        timeout = max(1.0, min(float(budget.process_timeout_seconds), math.floor(remaining)))
        run = self.s.measurer.measure(
            self.request,
            attempt_id=attempt.attempt_id,
            run_id=f"{attempt.attempt_id}-run{suffix}",
            candidate_digest=candidate_digest,
            timeout_seconds=timeout,
            cancel=self.control.cancel_event,
        )
        self.state.meter.process_runs += 1
        self.state.meter.output_bytes += run.output_bytes
        self._tick()
        return run

    def _current_for(
        self,
        observation: EnvironmentObservation,
        record: MeasurementRecord,
        expected_candidate: str | None = None,
    ) -> FreshnessVerdict:
        binding = record.binding
        assert binding is not None
        toolchain = (
            binding.toolchain.toolchain_digest
            if observation.launcher_digest == binding.toolchain.launcher_digest
            else digest("launcher-drift")
        )
        return check_freshness(
            binding,
            CurrentState(
                request_digest=self.rd,
                source_revision=observation.source_revision,
                production_digests=observation.production_digests,
                build_config_digest=observation.build_config_digest,
                toolchain_digest=toolchain,
                # Aday kimligi harness'in beklentisiyle karsilastirilir (kendisiyle degil).
                test_candidate_digest=(expected_candidate or binding.test_candidate_digest),
            ),
        )

    # -- baseline / rebaseline ----------------------------------------------------------

    def _measure_only_attempt(self) -> None:
        state = self.state
        first = state.baseline_obs is None
        kind = "baseline" if first else "rebaseline"
        observation = self.s.environment.observe()
        manifest = CandidateManifest(accepted_state_digest(state.accepted_files), (), kind)
        self._put(manifest.to_payload())
        plan_digest = state.plan.plan_digest if state.plan else digest({"plan": "none"})
        self._gate(where="claim")
        attempt = self._claim(manifest, plan_digest)
        run = self._run_measure(attempt, manifest.candidate_digest)
        if run.record is None:
            self._close(attempt, AttemptState.FAILED, {"blocked": list(run.blocked_details)}, ())
            raise self._stop(
                run.blocked_reason or UnitTestStopReason.ENVIRONMENT_MISSING,
                run.blocked_details,
            )
        record = run.record
        base_evidence = {"kind": kind, "measurement_digest": record.record_digest}
        if record.status in {MeasurementStatus.ACCEPTED, MeasurementStatus.NO_TESTS_BASELINE}:
            if record.binding is not None:
                verdict = self._current_for(observation, record, manifest.candidate_digest)
                if verdict is not FreshnessVerdict.FRESH:
                    self._close(
                        attempt,
                        AttemptState.FAILED,
                        {**base_evidence, "freshness": verdict.value},
                        (),
                    )
                    raise self._stop(
                        UnitTestStopReason.MEASUREMENT_INCOMPLETE, (f"freshness:{verdict.value}",)
                    )
            state.baseline_obs = observation
            state.needs_rebaseline = False
            observations = record.evidence.observations
            state.accepted_observations = {
                o.source_file: (o.covered, o.total)
                for o in observations
                if o.covered is not None and o.total is not None
            }
            state.accepted_measurement_digest = record.record_digest
            state.accepted_candidate_digest = manifest.candidate_digest
            state.accepted_plan_digest = state.plan.plan_digest if state.plan else None
            evaluation = record.evaluation
            met = evaluation is not None and evaluation.verdict is EvaluationVerdict.MET
            status = AttemptState.COMPLETED if observations else AttemptState.FAILED
            evidence_digest = self._close(
                attempt,
                status,
                {
                    **base_evidence,
                    "no_tests_baseline": record.status is MeasurementStatus.NO_TESTS_BASELINE,
                    "quality_verification": "ratio-observed-only",
                },
                observations,
            )
            if met and first:
                self._succeed(
                    UnitTestStopReason.ALREADY_AT_TARGET,
                    ("quality-not-independently-verified",),
                    record=record,
                    receipt_digest=evidence_digest,
                    expected_files={},
                    expected_candidate=manifest.candidate_digest,
                )
                return
            if met:
                self._try_finalize(record, evidence_digest)
            return
        classification = classify_failure(
            FailureSignals.from_record(
                record, failed_cases=run.failed_cases, output_tail=run.output_tail
            )
        )
        interrupted = record.status is MeasurementStatus.RUN_INCOMPLETE
        self._close(
            attempt,
            AttemptState.INTERRUPTED if interrupted else AttemptState.FAILED,
            {**base_evidence, "classification": classification.to_payload()},
            (),
        )
        if record.stop_reason is not None and interrupted:
            raise self._stop(record.stop_reason, (f"baseline:{record.status.value}",))
        reason = (
            UnitTestStopReason.ENVIRONMENT_MISSING
            if classification.kind is FailureKind.ENVIRONMENT
            else UnitTestStopReason.MEASUREMENT_INCOMPLETE
        )
        raise self._stop(
            reason,
            (f"{kind}:{record.status.value}", f"class:{classification.kind.value}"),
            extra={"classification": classification.to_payload()},
        )

    # -- plan ----------------------------------------------------------------------------

    def _meter_response(self, response: AgentResponse) -> None:
        meter = self.state.meter
        if self.s.gateway.remote:
            meter.remote_calls += 1
        meter.output_bytes += response.output_bytes
        meter.context_bytes += response.context_bytes
        meter.dispatch_failures = 0

    def _invoke(self, task: AgentTask) -> AgentResponse | None:
        """Basarisiz dagitim bounded sayilir; ``None`` = bu turda sonuc yok."""

        meter = self.state.meter
        if self.s.gateway.remote and meter.remote_calls >= self.limits.max_remote_calls:
            # B3: sayac dispatch'ten ONCE uygulanir; limit asilmaz.
            raise self._stop(UnitTestStopReason.BUDGET_EXHAUSTED, ("remote-calls",))
        try:
            response = self.s.gateway.invoke(task)
        except AgentDispatchFailure as exc:
            meter.dispatch_failures += 1
            if self.s.gateway.remote:
                meter.remote_calls += 1
            if meter.dispatch_failures > self.limits.max_dispatch_failures:
                # Politika reddi kurtarilabilir (resume/retry); digerleri ortam eksigi.
                reason = (
                    UnitTestStopReason.RECOVERY_REQUIRED
                    if exc.recoverable
                    else UnitTestStopReason.ENVIRONMENT_MISSING
                )
                raise self._stop(reason, ("agent-unavailable", exc.category)) from exc
            return None
        except PolicyViolation as exc:
            self.state.context_flag = True
            raise self._stop(
                UnitTestStopReason.ENVIRONMENT_MISSING, ("agent-context-refused", str(exc))
            ) from exc
        self._meter_response(response)
        return response

    def _scenario_context(self, scenarios: Any) -> list[dict[str, Any]]:
        return [s.to_payload() for s in scenarios]

    def _plan_step(self) -> bool:
        """Planner/analyzer cagrisi. ``True`` = plan hazir."""

        state = self.state
        if state.meter.replans > self.limits.max_replans:
            raise self._stop(UnitTestStopReason.STAGNATION_REVIEW, ("replan-limit",))
        task = AgentTask(
            specialty=AgentSpecialty.ANALYZER_PLANNER,
            request_digest=self.rd,
            ordinal=self._attempts() + 1,
            sequence=state.meter.replans,
            context={
                "request": self.request.to_payload(),
                "rejected_approaches": []
                if state.plan is None
                else list(state.plan.rejected_approach_digests),
                "unresolved": [] if state.plan is None else state.board.to_payload(),
                "failure_notes": {k: v[-3:] for k, v in sorted(state.failure_notes.items())},
                "defects": [d["scenario_ids"] for d in state.defects],
            },
            read_resources=self.request.source_files,
        )
        response = self._invoke(task)
        if response is None:
            return False
        try:
            body = parse_plan_body(response.body)
            version = 1 if state.plan is None else state.plan.version + 1
            plan = parse_behavior_plan(body.plan_document, self.request, version=version)
        except (ValidationFailed, PolicyViolation):
            state.meter.proposal_rejections += 1
            if state.meter.proposal_rejections > self.limits.max_proposal_rejections:
                raise self._stop(
                    UnitTestStopReason.STAGNATION_REVIEW, ("planner-invalid",)
                ) from None
            return False
        state.meter.proposal_rejections = 0
        rejected = () if state.plan is None else state.plan.rejected_approach_digests
        plan = plan.with_rejected(rejected)
        self._put(plan.to_payload())
        state.board = (
            state.board.carry_over(plan, keep_verified=True)
            if state.plan
            else ScenarioBoard.initial(plan)
        )
        state.plan = plan
        state.needs_replan = False
        state.context_gaps = list(body.context_gaps)
        if body.context_gaps:
            state.context_flag = True
        state.testability_findings = [dict(f) for f in body.testability_findings]
        return True

    # -- build attempt ------------------------------------------------------------------

    def _build_attempt(self, observation: EnvironmentObservation) -> None:
        state = self.state
        if (state.plan is None or state.needs_replan) and not self._plan_step():
            return
        assert state.plan is not None
        plan = state.plan
        actionable = actionable_scenarios(plan, state.board)
        if not actionable:
            self._no_actionable()
            return
        batch = actionable[: self.limits.batch_size]
        batch_ids = tuple(s.scenario_id for s in batch)
        policy = self._policy(observation)
        task = AgentTask(
            specialty=AgentSpecialty.BUILDER,
            request_digest=self.rd,
            ordinal=self._attempts() + 1,
            sequence=len(state.history),
            context={
                "scenarios": self._scenario_context(batch),
                "allowed_test_paths": list(self.request.allowed_test_paths),
                "forbidden": sorted(set(self.request.forbidden_paths) | state.frozen_paths),
                "owned_files": sorted(state.accepted_files.items()),
                "failure_notes": {sid: state.failure_notes.get(sid, [])[-3:] for sid in batch_ids},
                "rejected_approaches": list(plan.rejected_approach_digests),
                "required_strategy": None
                if state.forced_strategy is None
                else state.forced_strategy.value,
                "mode": "test-only",
            },
            read_resources=self.request.source_files,
            write_resources=self.request.allowed_test_paths,
        )
        response = self._invoke(task)
        if response is None:
            return
        state.last_builder_agent_id = response.envelope.agent_id
        try:
            body = parse_build_body(response.body, self.request)
            char_required = self._validate_proposal(body, batch_ids, policy, plan)
        except (ValidationFailed, GuardViolation, PolicyViolation) as exc:
            self._reject_proposal(batch_ids, StrategyKind.ALTERNATE_FIXTURE, exc)
            return
        if body.plan_delta is not None:
            if body.plan_delta.requires_authority:
                raise self._stop(
                    UnitTestStopReason.ENVIRONMENT_MISSING,
                    ("authority-required", *body.plan_delta.requires_authority[:4]),
                )
            state.plan = plan.with_scenarios(body.plan_delta.new_scenarios)
            state.board = state.board.carry_over(state.plan, keep_verified=True)
            plan = state.plan
        state.meter.proposal_rejections = 0
        state.forced_strategy = None
        self._execute_candidate(observation, plan, batch_ids, body, response, char_required)

    def _policy(self, observation: EnvironmentObservation) -> TestOnlyPolicy:
        return TestOnlyPolicy.for_request(
            self.request,
            frozen_paths=self.state.frozen_paths,
            protected_dirty_paths=observation.protected_dirty_paths,
            owned_paths=self.state.accepted_files,
        )

    def _validate_proposal(
        self,
        body: BuildBody,
        batch_ids: tuple[str, ...],
        policy: TestOnlyPolicy,
        plan: BehaviorPlan,
    ) -> tuple[str, ...]:
        for link in body.links:
            if link.scenario_id not in batch_ids:
                raise PolicyViolation("Link batch disi senaryoya baglanamaz")
        policy.check_patch(body.patch)
        if body.strategy is not StrategyKind.SETUP_DISCOVERY and not has_executable_test(
            body.patch
        ):
            raise PolicyViolation("Patch assertion'li calistirilabilir test tasimiyor")
        for change in body.patch.files:
            owned = self.state.accepted_files.get(change.path)
            if owned is not None and change.preimage_digest != owned:
                raise PolicyViolation(
                    "Sahip olunan dosya yalniz kabul edilmis preimage ile degisir"
                )
        return self._characterization_required(body, plan)

    def _characterization_required(self, body: BuildBody, plan: BehaviorPlan) -> tuple[str, ...]:
        """Karakterizasyon expected'ini 'onarmak' yeni, bagimsiz kanit ister.

        Oracle turu yalniz planner beyanidir: karakterizasyon assertion'i bozuldugunda veya
        kabul edilmis bir karakterizasyon testi degistirildiginde builder
        ``characterization_update``
        (bos olmayan kanit referansi) sunmali ve verifier ayrica onaylamalidir.
        """

        state = self.state
        characterization = {
            s.scenario_id for s in plan.scenarios if s.oracle_kind is OracleKind.CHARACTERIZATION
        }
        required = {link.scenario_id for link in body.links if link.scenario_id in state.char_gate}
        for change in body.patch.files:
            owned = state.accepted_files.get(change.path)
            if owned is not None and change.postimage_digest != owned:
                required |= {
                    sid
                    for sid in state.owned_scenarios.get(change.path, [])
                    if sid in characterization
                }
        provided = {sid for sid, ref in body.characterization_updates if ref.strip()}
        missing = required - provided
        if missing:
            raise GuardViolation(
                "characterization-update-evidence-missing", ",".join(sorted(missing))
            )
        return tuple(sorted(required))

    def _reject_proposal(
        self, batch_ids: tuple[str, ...], strategy: StrategyKind, exc: Exception
    ) -> None:
        state = self.state
        state.meter.proposal_rejections += 1
        approach = approach_digest(strategy, batch_ids, ())
        state.history.append(
            AttemptRecord(
                len(state.history) + 1,
                AttemptOutcomeKind.REJECTED,
                approach,
                self._gap(),
            )
        )
        if state.plan is not None:
            state.plan = state.plan.with_rejected([approach])
        reason = getattr(exc, "reason", type(exc).__name__)
        for sid in batch_ids:
            state.failure_notes.setdefault(sid, []).append(f"proposal-rejected:{reason}")
        if state.meter.proposal_rejections > self.limits.max_proposal_rejections:
            raise self._stop(
                UnitTestStopReason.STAGNATION_REVIEW, ("builder-policy-violations", str(reason))
            )

    def _gap(self) -> str:
        return state_gap_digest(self.state.accepted_observations, self.state.plan, self.state.board)

    def _revert(self, applied: AppliedPatch, manifest: CandidateManifest) -> RevertReceipt:
        preimages: dict[str, bytes] = {}
        for item in manifest.files:
            if item.preimage_digest is not None:
                preimages[item.path] = self.s.objects.get(item.preimage_digest)
        return self.s.workspace.revert(applied, preimages=preimages)

    def _store_candidate(self, patch_files: Any, manifest: CandidateManifest) -> None:
        existing = self.s.workspace.read_bytes(
            [c.path for c in patch_files if c.preimage_digest is not None]
        )
        for change in patch_files:
            if change.preimage_digest is not None:
                data = existing.get(change.path)
                if data is None or digest_of_bytes(data) != change.preimage_digest:
                    raise PreimageMismatch(change.path)
                self.s.objects.put(data, media_type="text/plain")
            self.s.objects.put(change.content_bytes, media_type="text/plain")
        stored = self._put(manifest.to_payload())
        if stored != manifest.candidate_digest:
            raise ValidationFailed("Aday manifest digest'i nesne deposuyla uyusmuyor")

    def _execute_candidate(
        self,
        observation: EnvironmentObservation,
        plan: BehaviorPlan,
        batch_ids: tuple[str, ...],
        body: BuildBody,
        builder: AgentResponse,
        char_required: tuple[str, ...] = (),
    ) -> None:
        state = self.state
        manifest = CandidateManifest.for_patch(
            accepted_state_digest(state.accepted_files), body.patch
        )
        approach = approach_digest(
            body.strategy, [link.scenario_id for link in body.links], body.patch.paths
        )
        state.tried_strategies.add(body.strategy)
        try:
            self._store_candidate(body.patch.files, manifest)
        except PreimageMismatch as exc:
            self._reject_proposal(batch_ids, body.strategy, exc)
            return
        self._gate(where="claim")  # B2: agent cagrisi sirasinda gelen cancel/pause claim engeller
        attempt = self._claim(manifest, plan.plan_digest)
        signal = self.control.signal()
        if signal is not ControlSignal.RUN:
            self._close(
                attempt, AttemptState.INTERRUPTED, {"signal": signal.value, "applied": False}, ()
            )
            reason = (
                UnitTestStopReason.USER_CANCELLED
                if signal is ControlSignal.CANCEL
                else UnitTestStopReason.USER_PAUSED
            )
            raise self._stop(reason, ("signal-before-apply",))
        try:
            applied = self.s.workspace.apply(body.patch, policy=self._policy(observation))
        except PreimageMismatch as exc:
            state.history.append(
                AttemptRecord(
                    len(state.history) + 1, AttemptOutcomeKind.FAILED, approach, self._gap()
                )
            )
            self._close(attempt, AttemptState.FAILED, {"apply": "refused", "path": exc.path}, ())
            return
        run = self._run_measure(attempt, manifest.candidate_digest)
        base: dict[str, Any] = {
            "kind": "build",
            "candidate_digest": manifest.candidate_digest,
            "scenario_ids": list(batch_ids),
            "strategy": body.strategy.value,
            "builder_envelope": builder.envelope_digest,
            "smell_candidates": [[c.code.value, c.path] for c in find_smell_candidates(body.patch)],
            "char_required": list(char_required),
        }
        links = [[link.scenario_id, link.test_ref] for link in body.links]
        base["links"] = links
        try:
            self._evaluate(
                observation,
                plan,
                batch_ids,
                body,
                builder,
                manifest,
                applied,
                attempt,
                run,
                approach,
                base,
            )
        except _Stop:
            if self.s.ledger.get_attempt_receipt(attempt.attempt_id) is None:
                # Acik kalan attempt: kendi patch'i geri alinir, receipt yazilir.
                receipt = self._revert(applied, manifest)
                self._close(
                    attempt,
                    AttemptState.INTERRUPTED,
                    {
                        **base,
                        "aborted": True,
                        "revert": {
                            "restored": list(receipt.restored),
                            "foreign_modified": list(receipt.foreign_modified),
                            "failed": list(receipt.failed),
                        },
                    },
                    (),
                )
            raise

    # -- degerlendirme ------------------------------------------------------------------

    def _finish_rejected(
        self,
        attempt: UnitTestAttempt,
        manifest: CandidateManifest,
        applied: AppliedPatch,
        approach: str,
        outcome: AttemptOutcomeKind,
        status: AttemptState,
        evidence: dict[str, Any],
        observations: tuple[Any, ...] = (),
        *,
        stale: bool = False,
    ) -> str:
        receipt = self._revert(applied, manifest)
        evidence["revert"] = {
            "restored": list(receipt.restored),
            "foreign_modified": list(receipt.foreign_modified),
            "failed": list(receipt.failed),
        }
        self.state.history.append(
            AttemptRecord(
                len(self.state.history) + 1,
                outcome,
                approach,
                self._gap(),
                (),
                stale_measurement=stale or self.state.stale_flag,
                scope_suspect=self.state.scope_flag,
                context_incomplete=self.state.context_flag,
            )
        )
        digest_value = self._close(attempt, status, evidence, observations)
        if receipt.failed:
            raise self._stop(UnitTestStopReason.RECOVERY_REQUIRED, ("revert-failed",))
        return digest_value

    def _evaluate(
        self,
        observation: EnvironmentObservation,
        plan: BehaviorPlan,
        batch_ids: tuple[str, ...],
        body: BuildBody,
        builder: AgentResponse,
        manifest: CandidateManifest,
        applied: AppliedPatch,
        attempt: UnitTestAttempt,
        run: MeasuredRun,
        approach: str,
        base: dict[str, Any],
    ) -> None:
        state = self.state
        record = run.record
        if record is None:
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                {**base, "blocked": list(run.blocked_details)},
            )
            raise self._stop(
                run.blocked_reason or UnitTestStopReason.ENVIRONMENT_MISSING, run.blocked_details
            )
        base["measurement_digest"] = record.record_digest
        if (
            record.status is MeasurementStatus.RUN_INCOMPLETE
            or record.status is MeasurementStatus.TOOL_MISSING
        ):
            # Cancel/timeout: patch sahipligi readback ile kanita yazilir, geri alinmaz.
            readback = self.s.workspace.digest_paths([p for p, _ in manifest.changed_files])
            state.history.append(
                AttemptRecord(
                    len(state.history) + 1, AttemptOutcomeKind.INTERRUPTED, approach, self._gap()
                )
            )
            self._close(
                attempt,
                AttemptState.INTERRUPTED,
                {**base, "ownership_readback": sorted((p, d or "") for p, d in readback.items())},
                (),
            )
            if record.status is MeasurementStatus.TOOL_MISSING:
                raise self._stop(UnitTestStopReason.ENVIRONMENT_MISSING, ("tool-missing",))
            raise self._stop(
                record.stop_reason or UnitTestStopReason.MEASUREMENT_INCOMPLETE,
                (f"run:{record.evidence.run_status.value}",),
            )
        if record.status is MeasurementStatus.ACCEPTED:
            self._accepted_measurement(
                observation,
                plan,
                batch_ids,
                body,
                builder,
                manifest,
                applied,
                attempt,
                record,
                approach,
                base,
            )
            return
        if record.status is MeasurementStatus.INTEGRATION_MIX:
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                {**base, "reason": "integration-mix"},
            )
            raise self._stop(UnitTestStopReason.MEASUREMENT_INCOMPLETE, ("integration-mix",))
        self._failed_measurement(
            plan, batch_ids, body, manifest, applied, attempt, record, run, approach, base
        )

    def _scenarios_for(self, cases: tuple[str, ...], body: BuildBody) -> tuple[str, ...]:
        matched: set[str] = set()
        for link in body.links:
            ref = link.test_ref
            if any(
                ref
                and (ref in case or case in ref or case.split("#")[0].endswith(ref.split("#")[0]))
                for case in cases
            ):
                matched.add(link.scenario_id)
        return tuple(sorted(matched or {link.scenario_id for link in body.links}))

    def _failed_measurement(
        self,
        plan: BehaviorPlan,
        batch_ids: tuple[str, ...],
        body: BuildBody,
        manifest: CandidateManifest,
        applied: AppliedPatch,
        attempt: UnitTestAttempt,
        record: MeasurementRecord,
        run: MeasuredRun,
        approach: str,
        base: dict[str, Any],
    ) -> None:
        state = self.state
        signals = FailureSignals.from_record(
            record, failed_cases=run.failed_cases, output_tail=run.output_tail
        )
        involved = self._scenarios_for(signals.failed_cases, body)
        oracles = {sid: plan.scenario(sid).oracle_kind for sid in involved}
        classification = classify_failure(signals, scenario_oracles=oracles)
        repeats: FlakyDiagnosis | None = None
        if classification.kind in {
            FailureKind.FIXTURE,
            FailureKind.EXPECTATION,
            FailureKind.PRODUCTION_DEFECT,
            FailureKind.UNKNOWN,
        }:
            repeats = self._diagnose_repeats(attempt, manifest, record, run)
            if repeats is not None:
                classification = classify_failure(
                    signals, scenario_oracles=oracles, repeats=repeats
                )
                state.flaky_diagnoses.append(
                    {"attempt": attempt.attempt_id, **repeats.to_payload()}
                )
        evidence = {
            **base,
            "classification": classification.to_payload(),
            "signature": signals.signature_digest(),
        }
        if repeats is not None:
            evidence["repeats"] = repeats.to_payload()
        kind = classification.kind
        if kind is FailureKind.PRODUCTION_DEFECT:
            self._handle_defect(
                classification, body, manifest, applied, attempt, signals, approach, evidence
            )
            return
        if kind is FailureKind.FLAKY:
            for sid in involved:
                state.board = state.board.with_state(sid, ScenarioState.FLAKY_QUARANTINED, "flaky")
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                evidence,
            )
            return
        if kind is FailureKind.ENVIRONMENT:
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                evidence,
            )
            raise self._stop(
                UnitTestStopReason.ENVIRONMENT_MISSING,
                ("class:environment",),
                extra={"classification": classification.to_payload()},
            )
        if kind is FailureKind.MEASUREMENT_PARSER:
            state.stale_flag = True
            failures = state.meter.extra.get("parser_failures", 0) + 1
            state.meter.extra["parser_failures"] = failures
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                evidence,
                stale=True,
            )
            if failures >= 2:
                raise self._stop(
                    UnitTestStopReason.MEASUREMENT_INCOMPLETE, ("measurement-parser-repeated",)
                )
            state.needs_rebaseline = True
            return
        if kind is FailureKind.UNKNOWN:
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                evidence,
            )
            raise self._stop(
                UnitTestStopReason.STAGNATION_REVIEW,
                ("unknown-failure-escalation",),
                extra={"classification": classification.to_payload()},
            )
        if kind is FailureKind.EXPECTATION:
            for sid in involved:
                if plan.scenario(sid).oracle_kind is OracleKind.CHARACTERIZATION:
                    state.char_gate.add(sid)
        # onarilabilir: yalniz builder yolu; analyzer/planner yeniden cagrilmaz
        for sid in involved or batch_ids:
            count = state.repair_counts.get(sid, 0) + 1
            state.repair_counts[sid] = count
            state.failure_notes.setdefault(sid, []).append(
                f"{kind.value}:{signals.signature_digest()[-12:]}"
            )
            if count > self.limits.max_repair_per_scenario:
                state.board = state.board.with_state(sid, ScenarioState.UNRESOLVED, "repair-limit")
        if state.plan is not None:
            state.plan = state.plan.with_rejected([approach])
        self._finish_rejected(
            attempt,
            manifest,
            applied,
            approach,
            AttemptOutcomeKind.FAILED,
            AttemptState.FAILED,
            evidence,
        )

    def _diagnose_repeats(
        self,
        attempt: UnitTestAttempt,
        manifest: CandidateManifest,
        record: MeasurementRecord,
        run: MeasuredRun,
    ) -> FlakyDiagnosis | None:
        """Tani amacli sabit sayida tekrar; yesile kadar retry YOKTUR, sira kaydedilir."""

        runs = [RepeatRun(1, "initial", record.evidence.test_kind, run.failed_cases)]
        meter = self.state.meter
        for _ in range(self.limits.flaky_repeats):
            if meter.diagnostic_runs >= self.limits.max_diagnostic_runs:
                break
            if self.control.signal() is ControlSignal.CANCEL:
                break
            meter.diagnostic_runs += 1
            again = self._run_measure(
                attempt, manifest.candidate_digest, suffix=f"-diag{meter.diagnostic_runs}"
            )
            if again.record is None:
                break
            runs.append(
                RepeatRun(
                    len(runs) + 1, "same-order", again.record.evidence.test_kind, again.failed_cases
                )
            )
        return FlakyDiagnosis(tuple(runs)) if len(runs) >= 2 else None

    def _handle_defect(
        self,
        classification: FailureClassification,
        body: BuildBody,
        manifest: CandidateManifest,
        applied: AppliedPatch,
        attempt: UnitTestAttempt,
        signals: FailureSignals,
        approach: str,
        evidence: dict[str, Any],
    ) -> None:
        state = self.state
        assert state.plan is not None
        plan = state.plan
        triage_digest: str | None = None
        finding = "inconclusive"
        task = AgentTask(
            specialty=AgentSpecialty.DEFECT_TRIAGE,
            request_digest=self.rd,
            ordinal=attempt.ordinal,
            sequence=0,
            context={
                "scenarios": self._scenario_context(
                    [plan.scenario(s) for s in classification.scenario_ids]
                ),
                "failed_cases": list(signals.failed_cases),
                "signature": signals.signature_digest(),
                "candidate_digest": manifest.candidate_digest,
                "patch": [[f.path, f.content] for f in body.patch.files],
                "independent_of_builder": state.last_builder_agent_id,
            },
            risk="high",
        )
        response = self._invoke(task)
        if response is not None:
            problem = self._independence_problem(response)
            if problem is not None:
                self._finish_rejected(
                    attempt,
                    manifest,
                    applied,
                    approach,
                    AttemptOutcomeKind.FAILED,
                    AttemptState.FAILED,
                    {**evidence, "independence": problem},
                )
                raise self._stop(UnitTestStopReason.ENVIRONMENT_MISSING, (problem,))
            try:
                review = parse_verify_body(response.body, self.request).defect_review
            except (ValidationFailed, PolicyViolation):
                review = None
            triage_digest = response.envelope_digest
            if review is not None:
                finding = review.finding
        evidence["defect_triage"] = {"finding": finding, "envelope": triage_digest}
        if finding == "test-wrong":
            for sid in classification.scenario_ids:
                state.failure_notes.setdefault(sid, []).append("test-wrong:triage")
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                evidence,
            )
            return
        reproducers = _reproducer_paths(signals.failed_cases, body.patch.paths)
        proposal = DefectProposal(
            scenario_ids=classification.scenario_ids,
            oracle_refs=tuple(plan.scenario(s).oracle_ref for s in classification.scenario_ids),
            reproducer_candidate_digest=manifest.candidate_digest,
            reproducer_paths=reproducers,
            signature_digest=signals.signature_digest(),
            failed_cases=signals.failed_cases,
            triage_envelope_digest=triage_digest,
        )
        state.defects.append(proposal.to_payload())
        state.frozen_paths |= set(reproducers)
        for sid in classification.scenario_ids:
            state.board = state.board.with_state(
                sid, ScenarioState.PRODUCTION_DEFECT, "defect-proposal"
            )
        evidence["defect_proposal_digest"] = proposal.proposal_digest
        self._finish_rejected(
            attempt,
            manifest,
            applied,
            approach,
            AttemptOutcomeKind.DEFECT,
            AttemptState.FAILED,
            evidence,
        )

    def _independence_problem(self, verifier: AgentResponse) -> str | None:
        """Builder kendi degisikliginin verifier'i olamaz: ayri agent kimligi ve rol sart."""

        builder_agent = self.state.last_builder_agent_id
        if builder_agent is not None and verifier.envelope.agent_id == builder_agent:
            return "verifier-not-independent"
        if verifier.specialty not in VERIFYING:
            return "verifier-role-invalid"
        return None

    def _assert_independent(self, verifier: AgentResponse) -> None:
        problem = self._independence_problem(verifier)
        if problem is not None:
            raise self._stop(UnitTestStopReason.ENVIRONMENT_MISSING, (problem,))

    # -- kabul ---------------------------------------------------------------------------

    def _accepted_measurement(
        self,
        observation: EnvironmentObservation,
        plan: BehaviorPlan,
        batch_ids: tuple[str, ...],
        body: BuildBody,
        builder: AgentResponse,
        manifest: CandidateManifest,
        applied: AppliedPatch,
        attempt: UnitTestAttempt,
        record: MeasurementRecord,
        approach: str,
        base: dict[str, Any],
    ) -> None:
        state = self.state
        expected = {**state.accepted_files, **dict(manifest.changed_files)}
        stale = self._stale_reasons(record, expected, manifest.candidate_digest)
        if stale:
            state.stale_flag = True
            if any(r.startswith(("drift:", "freshness:")) for r in stale):
                state.needs_rebaseline = True
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                {**base, "freshness": list(stale)},
                stale=True,
            )
            return
        task = AgentTask(
            specialty=AgentSpecialty.VERIFIER,
            request_digest=self.rd,
            ordinal=attempt.ordinal,
            sequence=0,
            context={
                "scenarios": self._scenario_context([plan.scenario(s) for s in batch_ids]),
                "links": base["links"],
                "patch": [[f.path, f.content] for f in body.patch.files],
                "smell_candidates": base["smell_candidates"],
                "measurement_digest": record.record_digest,
                "counts": [list(c) for c in record.evidence.test_counts],
                "builder_envelope_digest": builder.envelope_digest,
                "candidate_digest": manifest.candidate_digest,
            },
            risk="high",
        )
        response = self._invoke(task)
        if response is None:
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                {**base, "verifier": "unavailable"},
            )
            return
        problem = self._independence_problem(response)
        if (response.assignment_id, response.invocation_id) == (
            builder.assignment_id,
            builder.invocation_id,
        ):
            problem = "verifier-same-invocation"
        if problem is not None:
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.FAILED,
                AttemptState.FAILED,
                {**base, "independence": problem},
            )
            raise self._stop(UnitTestStopReason.ENVIRONMENT_MISSING, (problem,))
        try:
            verdict_body = parse_verify_body(response.body, self.request)
        except (ValidationFailed, PolicyViolation) as exc:
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.REJECTED,
                AttemptState.COMPLETED,
                {**base, "verifier": f"invalid:{type(exc).__name__}"},
                record.evidence.observations,
            )
            return
        evidence = {
            **base,
            "verifier_envelope": response.envelope_digest,
            "verdict": verdict_body.effective_verdict.value,
            "quality_codes": list(verdict_body.quality_codes),
        }
        effective = verdict_body.effective_verdict
        observations = record.evidence.observations
        char_required = set(base.get("char_required", []))
        unconfirmed = char_required - set(verdict_body.characterization_updates)
        quality_codes = verdict_body.quality_codes
        if effective is Verdict.ACCEPTED and unconfirmed:
            effective = Verdict.REJECTED
            quality_codes = (*quality_codes, "characterization-update-unconfirmed")
            evidence["unconfirmed_characterization"] = sorted(unconfirmed)
        if effective is Verdict.ACCEPTED:
            # B1: verifier cagrisi sirasinda disk degismis olabilir; kabulden once yeniden dogrula.
            stale_after = self._stale_reasons(record, expected, manifest.candidate_digest)
            if stale_after:
                state.stale_flag = True
                if any(r.startswith(("drift:", "freshness:")) for r in stale_after):
                    state.needs_rebaseline = True
                self._finish_rejected(
                    attempt,
                    manifest,
                    applied,
                    approach,
                    AttemptOutcomeKind.FAILED,
                    AttemptState.FAILED,
                    {**evidence, "freshness_after_verifier": list(stale_after)},
                    stale=True,
                )
                return
            self._abort_on_signal(attempt, manifest, applied, approach, evidence)
        if effective is not Verdict.ACCEPTED:
            if effective is Verdict.NEEDS_REPLAN:
                state.needs_replan = True
                state.meter.replans += 1
            for sid in batch_ids:
                state.failure_notes.setdefault(sid, []).append(
                    "verifier-rejected:" + ",".join(quality_codes[:3])
                )
                state.repair_counts[sid] = state.repair_counts.get(sid, 0) + 1
                if state.repair_counts[sid] > self.limits.max_repair_per_scenario:
                    state.board = state.board.with_state(
                        sid, ScenarioState.UNRESOLVED, "quality-limit"
                    )
            if state.plan is not None:
                state.plan = state.plan.with_rejected([approach])
            self._finish_rejected(
                attempt,
                manifest,
                applied,
                approach,
                AttemptOutcomeKind.REJECTED,
                AttemptState.COMPLETED,
                evidence,
                observations,
            )
            return
        # kabul: kendi patch'imiz sahipligimiz altinda kalir (drift degil)
        linked = {link.scenario_id for link in body.links}
        for sid in verdict_body.verified:
            if sid in linked:
                state.board = state.board.with_state(sid, ScenarioState.VERIFIED, "verifier")
        for sid in verdict_body.unresolved:
            if sid in linked:
                state.board = state.board.with_state(sid, ScenarioState.UNRESOLVED, "verifier")
        gains: list[ProgressGain] = []
        new_obs = {
            o.source_file: (o.covered, o.total)
            for o in observations
            if o.covered is not None and o.total is not None
        }
        for source, (covered, total) in new_obs.items():
            before = state.accepted_observations.get(source)
            if before is not None and before[1] == total and covered > before[0]:
                gains.append(ProgressGain(ProgressKind.NEW_COVERED_COUNT, record.record_digest))
                break
        for sid in verdict_body.verified:
            if sid in linked:
                gains.append(ProgressGain(ProgressKind.VERIFIED_SCENARIO, response.envelope_digest))
        if verdict_body.fixture_ready and body.strategy is StrategyKind.SETUP_DISCOVERY:
            gains.append(
                ProgressGain(ProgressKind.VERIFIED_FIXTURE_READINESS, response.envelope_digest)
            )
        for item in manifest.files:
            state.accepted_files[item.path] = item.postimage_digest
            state.owned_scenarios[item.path] = _scenarios_for_path(item.path, body.links)
        state.accepted_observations = new_obs
        state.accepted_measurement_digest = record.record_digest
        state.accepted_candidate_digest = manifest.candidate_digest
        state.char_gate -= char_required
        if verdict_body.new_material_risks and state.plan is not None:
            state.plan = state.plan.with_scenarios(verdict_body.new_material_risks)
            state.board = state.board.carry_over(state.plan, keep_verified=True)
        state.accepted_plan_digest = state.plan.plan_digest if state.plan else None
        state.stale_flag = False
        state.history.append(
            AttemptRecord(
                len(state.history) + 1,
                AttemptOutcomeKind.ACCEPTED,
                approach,
                self._gap(),
                tuple(gains),
            )
        )
        state.forced_strategy = None
        evidence["gains"] = [[g.kind.value, g.evidence_digest] for g in gains]
        evidence["value_notes"] = list(verdict_body.value_notes)
        evidence_digest = self._close(attempt, AttemptState.COMPLETED, evidence, observations)
        evaluation = record.evaluation
        if evaluation is not None and evaluation.verdict is EvaluationVerdict.MET:
            self._try_finalize(record, evidence_digest)

    def _abort_on_signal(
        self,
        attempt: UnitTestAttempt,
        manifest: CandidateManifest,
        applied: AppliedPatch,
        approach: str,
        evidence: dict[str, Any],
    ) -> None:
        """Kabulden once cancel/pause: cancel sahiplik kaniti birakir, pause geri alir."""

        signal = self.control.signal()
        if signal is ControlSignal.RUN:
            return
        if signal is ControlSignal.CANCEL:
            readback = self.s.workspace.digest_paths([p for p, _ in manifest.changed_files])
            self._close(
                attempt,
                AttemptState.INTERRUPTED,
                {
                    **evidence,
                    "ownership_readback": sorted((p, d or "") for p, d in readback.items()),
                },
                (),
            )
            raise self._stop(UnitTestStopReason.USER_CANCELLED, ("cancel-before-acceptance",))
        self._finish_rejected(
            attempt,
            manifest,
            applied,
            approach,
            AttemptOutcomeKind.INTERRUPTED,
            AttemptState.INTERRUPTED,
            dict(evidence),
        )
        raise self._stop(UnitTestStopReason.USER_PAUSED, ("pause-before-acceptance",))

    # -- final ---------------------------------------------------------------------------

    def _no_actionable(self) -> None:
        state = self.state
        assert state.plan is not None
        if state.defects:
            raise self._stop(UnitTestStopReason.PRODUCTION_DEFECT, ("defect-proposals",))
        if scenarios_in_state(state.plan, state.board, ScenarioState.NEEDS_SPECIFICATION):
            raise self._stop(UnitTestStopReason.SPEC_AMBIGUOUS, ("needs-specification",))
        if state.meter.replans >= self.limits.max_replans:
            raise self._stop(UnitTestStopReason.STAGNATION_REVIEW, ("no-actionable-scenarios",))
        state.meter.replans += 1
        state.needs_replan = True

    def _try_finalize(self, record: MeasurementRecord, evidence_digest: str) -> None:
        state = self.state
        assert record.binding is not None and state.plan is not None
        plan = state.plan
        if state.defects:
            raise self._stop(UnitTestStopReason.PRODUCTION_DEFECT, ("defect-proposals",))
        if unresolved_material(plan, state.board):
            return
        stale = self._stale_reasons(record, state.accepted_files, state.accepted_candidate_digest)
        if stale:
            self._mark_stale(stale)
            return
        if state.meter.extra.get("final_rejections", 0) > self.limits.max_replans:
            raise self._stop(UnitTestStopReason.STAGNATION_REVIEW, ("final-verification-rejected",))
        task = AgentTask(
            specialty=AgentSpecialty.FINAL_VERIFIER,
            request_digest=self.rd,
            ordinal=self._attempts(),
            sequence=state.meter.extra.get("final_rejections", 0),
            context={
                "candidate_digest": record.binding.test_candidate_digest,
                "measurement_digest": record.record_digest,
                "accepted_files": sorted(state.accepted_files.items()),
                "scenarios": [
                    [s.scenario_id, state.board.state_of(s.scenario_id).value]
                    for s in plan.scenarios
                ],
                "last_builder_agent_id": state.last_builder_agent_id,
            },
            risk="high",
        )
        response = self._invoke(task)
        if response is None:
            return
        self._assert_independent(response)
        try:
            final = parse_verify_body(response.body, self.request)
        except (ValidationFailed, PolicyViolation):
            state.meter.extra["final_rejections"] = state.meter.extra.get("final_rejections", 0) + 1
            return
        if final.new_material_risks:
            state.plan = plan.with_scenarios(final.new_material_risks)
            state.board = state.board.carry_over(state.plan, keep_verified=True)
            return
        if final.effective_verdict is not Verdict.ACCEPTED:
            state.meter.extra["final_rejections"] = state.meter.extra.get("final_rejections", 0) + 1
            state.needs_replan = True
            return
        self._succeed(
            UnitTestStopReason.TARGET_REACHED,
            ("final-verification-accepted",),
            record=record,
            receipt_digest=evidence_digest,
            expected_files=state.accepted_files,
            expected_candidate=state.accepted_candidate_digest,
            extra={
                "final_verifier_envelope": response.envelope_digest,
                "measurement_digest": record.record_digest,
                "freshness": "re-verified-from-disk-before-terminal",
            },
        )
