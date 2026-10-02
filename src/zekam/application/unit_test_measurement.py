"""Unit-test olcumunun taze kaynak/rapor baglama ve kabul karari (W04, M07, M08, M12, M13).

Saf ve I/O'suz: runner/parser ciktisi ``MeasurementEvidence`` olarak verilir; burada
yalniz baglama, tazelik ve kabul/red karari uretilir. Dosya mtime'i tazelik kaniti
degildir; baglama source revision, hedef production digest'i, test aday digest'i,
build/config/toolchain kimligi, run/attempt ve rapor digest'lerinin tamamindan olusur.

Kendi accepted test patch'i (``test_candidate_digest`` degisimi) harici drift degildir:
``CANDIDATE_MISMATCH`` ayri verdict'tir ve ``is_external_drift`` False doner; production
digest/revision/config/toolchain degisimi ise harici drift'tir.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.unit_test_engineering import (
    CoverageObservation,
    EvaluationVerdict,
    ScopeEvaluation,
    UnitTestRequest,
    UnitTestStopReason,
    evaluate_scope,
)

MEASUREMENT_CONTRACT: Final = "zekam-unit-test-measurement/v1"


class MeasurementScope(StrEnum):
    UNIT = "unit"
    INTEGRATION = "integration"


class RunStatus(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "timed-out"
    CANCELLED = "cancelled"
    OUTPUT_OVERFLOW = "output-overflow"
    LAUNCH_FAILED = "launch-failed"
    TOOL_MISSING = "tool-missing"


class TestRunKind(StrEnum):
    """Surefire ozetinin kabul acisindan sinifi (parser enum'undan bagimsiz sozlesme)."""

    __test__ = False

    PASSED = "passed"
    NO_TESTS = "no-tests"
    ALL_SKIPPED = "all-skipped"
    FAILED = "failed"
    INCONSISTENT_REPORT = "inconsistent-report"
    FLAKY_SUSPECTED = "flaky-suspected"
    INTEGRATION_TESTS_PRESENT = "integration-tests-present"
    UNAVAILABLE = "unavailable"


class FreshnessVerdict(StrEnum):
    FRESH = "fresh"
    REQUEST_MISMATCH = "request-mismatch"
    SCOPE_MISMATCH = "scope-mismatch"
    SOURCE_REVISION_DRIFT = "source-revision-drift"
    PRODUCTION_DRIFT = "production-drift"
    CONFIG_DRIFT = "config-drift"
    TOOLCHAIN_DRIFT = "toolchain-drift"
    CANDIDATE_MISMATCH = "candidate-mismatch"

    @property
    def is_external_drift(self) -> bool:
        return self in {
            FreshnessVerdict.SOURCE_REVISION_DRIFT,
            FreshnessVerdict.PRODUCTION_DRIFT,
            FreshnessVerdict.CONFIG_DRIFT,
            FreshnessVerdict.TOOLCHAIN_DRIFT,
        }


class MeasurementStatus(StrEnum):
    ACCEPTED = "accepted"
    NO_TESTS_BASELINE = "no-tests-baseline"
    TESTS_FAILED = "tests-failed"
    TESTS_NOT_ACCEPTABLE = "tests-not-acceptable"
    INTEGRATION_MIX = "integration-mix"
    UNBOUND_OR_STALE = "unbound-or-stale"
    TOOL_MISSING = "tool-missing"
    RUN_INCOMPLETE = "run-incomplete"
    MEASUREMENT_INCOMPLETE = "measurement-incomplete"


def _sorted_pairs(pairs: Iterable[tuple[str, str]], label: str) -> tuple[tuple[str, str], ...]:
    result = tuple(sorted((str(k), str(v)) for k, v in pairs))
    if len({key for key, _ in result}) != len(result):
        raise ValidationFailed(f"{label} tekrar eden anahtar")
    return result


@dataclass(frozen=True, slots=True)
class ToolchainIdentity:
    """Arac zinciri kimligi; derleme ``release`` ile gercek test JVM'i ayri tutulur."""

    launcher_kind: str
    launcher_digest: str
    maven_version: str | None
    test_jvm_version: str | None
    compile_release: str | None
    plugin_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        parse_digest(self.launcher_digest)
        object.__setattr__(
            self, "plugin_versions", _sorted_pairs(self.plugin_versions, "plugin_versions")
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "launcher_kind": self.launcher_kind,
            "launcher_digest": self.launcher_digest,
            "maven_version": self.maven_version,
            "test_jvm_version": self.test_jvm_version,
            "compile_release": self.compile_release,
            "plugin_versions": [list(item) for item in self.plugin_versions],
        }

    @property
    def toolchain_digest(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class MeasurementBinding:
    """Bir olcumu tam kaynak/rapor kimligine baglar."""

    request_digest: str
    attempt_id: str
    run_id: str
    scope: MeasurementScope
    source_revision: str
    production_digests: tuple[tuple[str, str], ...]
    class_digests: tuple[tuple[str, str], ...]
    test_candidate_digest: str
    build_config_digest: str
    plan_digest: str
    toolchain: ToolchainIdentity
    report_digests: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for label in ("request_digest", "test_candidate_digest", "build_config_digest"):
            parse_digest(getattr(self, label))
        parse_digest(self.plan_digest)
        if not self.attempt_id or not self.run_id or not self.source_revision.strip():
            raise ValidationFailed("attempt/run/source_revision bos olamaz")
        for label in ("production_digests", "class_digests", "report_digests"):
            pairs = _sorted_pairs(getattr(self, label), label)
            for _, value in pairs:
                parse_digest(value)
            object.__setattr__(self, label, pairs)
        if not self.production_digests:
            raise ValidationFailed("Hedef production digest'i olmadan baglama yok")
        if not self.report_digests:
            raise ValidationFailed("Rapor digest'i olmadan baglama yok")

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract": MEASUREMENT_CONTRACT,
            "request_digest": self.request_digest,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "scope": self.scope.value,
            "source_revision": self.source_revision,
            "production_digests": [list(i) for i in self.production_digests],
            "class_digests": [list(i) for i in self.class_digests],
            "test_candidate_digest": self.test_candidate_digest,
            "build_config_digest": self.build_config_digest,
            "plan_digest": self.plan_digest,
            "toolchain": self.toolchain.to_payload(),
            "report_digests": [list(i) for i in self.report_digests],
        }

    @property
    def binding_digest(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class CurrentState:
    """Olcumun gecerli kalip kalmadigini sormak icin guncel kaynak durumu."""

    request_digest: str
    source_revision: str
    production_digests: Mapping[str, str]
    build_config_digest: str
    toolchain_digest: str
    test_candidate_digest: str


def check_freshness(binding: MeasurementBinding, current: CurrentState) -> FreshnessVerdict:
    """Sabit sirayla ilk uyumsuzlugu doner; mtime kullanilmaz."""

    if binding.request_digest != current.request_digest:
        return FreshnessVerdict.REQUEST_MISMATCH
    if binding.scope is not MeasurementScope.UNIT:
        return FreshnessVerdict.SCOPE_MISMATCH
    if binding.source_revision != current.source_revision:
        return FreshnessVerdict.SOURCE_REVISION_DRIFT
    if dict(binding.production_digests) != dict(current.production_digests):
        return FreshnessVerdict.PRODUCTION_DRIFT
    if binding.build_config_digest != current.build_config_digest:
        return FreshnessVerdict.CONFIG_DRIFT
    if binding.toolchain.toolchain_digest != current.toolchain_digest:
        return FreshnessVerdict.TOOLCHAIN_DRIFT
    if binding.test_candidate_digest != current.test_candidate_digest:
        return FreshnessVerdict.CANDIDATE_MISMATCH
    return FreshnessVerdict.FRESH


@dataclass(frozen=True, slots=True)
class MeasurementEvidence:
    """Runner/parser'in normalize cikti ozeti (model beyani degil)."""

    run_status: RunStatus
    exit_code: int | None
    test_kind: TestRunKind
    test_counts: tuple[tuple[str, int], ...]
    observations: tuple[CoverageObservation, ...]
    integration_artifacts_changed: bool = False
    unexpected_plugin_executions: tuple[str, ...] = ()
    missing_expected_plugins: tuple[str, ...] = ()
    stale_report_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MeasurementRecord:
    binding: MeasurementBinding | None
    status: MeasurementStatus
    stop_reason: UnitTestStopReason | None
    evidence: MeasurementEvidence
    evaluation: ScopeEvaluation | None
    blockers: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        evaluation = self.evaluation
        return {
            "contract": MEASUREMENT_CONTRACT,
            "binding_digest": self.binding.binding_digest if self.binding else None,
            "status": self.status.value,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "run_status": self.evidence.run_status.value,
            "exit_code": self.evidence.exit_code,
            "test_kind": self.evidence.test_kind.value,
            "test_counts": [list(item) for item in self.evidence.test_counts],
            "observations": [o.to_payload() for o in self.evidence.observations],
            "verdict": evaluation.verdict.value if evaluation else None,
            "aggregate": (
                [evaluation.aggregate_covered, evaluation.aggregate_total] if evaluation else None
            ),
            "blockers": list(self.blockers),
        }

    @property
    def record_digest(self) -> str:
        return digest(self.to_payload())


def _record(
    binding: MeasurementBinding | None,
    status: MeasurementStatus,
    reason: UnitTestStopReason | None,
    evidence: MeasurementEvidence,
    blockers: Iterable[str],
    evaluation: ScopeEvaluation | None = None,
) -> MeasurementRecord:
    return MeasurementRecord(binding, status, reason, evidence, evaluation, tuple(blockers))


_TEST_KIND_STATUS: Final = {
    TestRunKind.NO_TESTS: MeasurementStatus.NO_TESTS_BASELINE,
    TestRunKind.FAILED: MeasurementStatus.TESTS_FAILED,
    TestRunKind.ALL_SKIPPED: MeasurementStatus.TESTS_NOT_ACCEPTABLE,
    TestRunKind.INCONSISTENT_REPORT: MeasurementStatus.TESTS_NOT_ACCEPTABLE,
    TestRunKind.FLAKY_SUSPECTED: MeasurementStatus.TESTS_NOT_ACCEPTABLE,
    TestRunKind.UNAVAILABLE: MeasurementStatus.TESTS_NOT_ACCEPTABLE,
    TestRunKind.INTEGRATION_TESTS_PRESENT: MeasurementStatus.INTEGRATION_MIX,
}


def assemble_measurement(
    request: UnitTestRequest,
    binding: MeasurementBinding | None,
    evidence: MeasurementEvidence,
    *,
    freshness: FreshnessVerdict | None = None,
) -> MeasurementRecord:
    """Kabul karari. Coverage yalniz taze, bagli, unit-scope ve gecen testlerle degerlendirilir.

    ``freshness`` verilirse (kaydin ilk uretim aninda guncel durumla karsilastirmasi)
    FRESH disindaki her deger olcumu reddeder.
    """

    run = evidence.run_status
    if run is RunStatus.TOOL_MISSING:
        return _record(
            binding,
            MeasurementStatus.TOOL_MISSING,
            UnitTestStopReason.ENVIRONMENT_MISSING,
            evidence,
            ["maven/launcher bulunamadi"],
        )
    if run is not RunStatus.COMPLETED:
        reason = (
            UnitTestStopReason.USER_CANCELLED
            if run is RunStatus.CANCELLED
            else UnitTestStopReason.MEASUREMENT_INCOMPLETE
        )
        return _record(
            binding, MeasurementStatus.RUN_INCOMPLETE, reason, evidence, [f"run: {run.value}"]
        )
    incomplete = UnitTestStopReason.MEASUREMENT_INCOMPLETE
    if binding is None:
        return _record(
            None, MeasurementStatus.UNBOUND_OR_STALE, incomplete, evidence, ["baglama yok"]
        )
    if binding.request_digest != request.request_digest:
        return _record(
            binding, MeasurementStatus.UNBOUND_OR_STALE, incomplete, evidence, ["istek digest'i"]
        )
    if freshness is not None and freshness is not FreshnessVerdict.FRESH:
        return _record(
            binding,
            MeasurementStatus.UNBOUND_OR_STALE,
            incomplete,
            evidence,
            [f"freshness: {freshness.value}"],
        )
    if evidence.stale_report_paths:
        return _record(
            binding,
            MeasurementStatus.UNBOUND_OR_STALE,
            incomplete,
            evidence,
            [f"stale rapor: {path}" for path in evidence.stale_report_paths],
        )
    if binding.scope is not MeasurementScope.UNIT or evidence.integration_artifacts_changed:
        return _record(
            binding,
            MeasurementStatus.INTEGRATION_MIX,
            incomplete,
            evidence,
            ["integration/unit karisimi"],
        )
    status = _TEST_KIND_STATUS.get(evidence.test_kind)
    if status is not None:
        reason_for_status = None if status is MeasurementStatus.NO_TESTS_BASELINE else incomplete
        return _record(
            binding, status, reason_for_status, evidence, [f"testler: {evidence.test_kind.value}"]
        )
    surprises = [f"beklenmeyen plugin: {p}" for p in evidence.unexpected_plugin_executions]
    if evidence.exit_code != 0:
        surprises.append(f"build exit code {evidence.exit_code}")
    surprises += [f"eksik plugin: {p}" for p in evidence.missing_expected_plugins]
    if surprises:
        return _record(
            binding, MeasurementStatus.MEASUREMENT_INCOMPLETE, incomplete, evidence, surprises
        )
    evaluation = evaluate_scope(request, evidence.observations)
    if evaluation.verdict is EvaluationVerdict.INCONCLUSIVE:
        return _record(
            binding,
            MeasurementStatus.MEASUREMENT_INCOMPLETE,
            incomplete,
            evidence,
            evaluation.blockers,
            evaluation,
        )
    return _record(binding, MeasurementStatus.ACCEPTED, None, evidence, (), evaluation)
