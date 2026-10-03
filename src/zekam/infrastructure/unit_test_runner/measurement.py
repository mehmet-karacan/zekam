"""Calistirma + taze rapor toplama + baglama + kabul: uctan uca unit-test olcumu (W04).

Akis: plan (hazir degilse ``PlanBlocked``) -> ``run_maven`` (yetki, kilit, karantina) ->
yalniz bu kosuda olusan rapor/exec dosyalari -> Surefire/JaCoCo parser'lari ->
``MeasurementBinding`` -> ``assemble_measurement`` kabul karari. Model beyani hicbir
adimda sayac veya sonuc kaynagi degildir.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from zekam.application.unit_test_measurement import (
    FreshnessVerdict,
    MeasurementBinding,
    MeasurementEvidence,
    MeasurementRecord,
    MeasurementScope,
    RunStatus,
    TestRunKind,
    ToolchainIdentity,
    assemble_measurement,
)
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.unit_test_engineering import (
    CoverageObservation,
    CoverageState,
    UnitTestRequest,
    UnitTestStopReason,
)
from zekam.infrastructure.unit_test_runner.jacoco_report import (
    JacocoReport,
    TargetMappingError,
    build_observations,
    parse_jacoco_report,
    resolve_java_target,
)
from zekam.infrastructure.unit_test_runner.maven_plan import (
    ExecutionAuthorization,
    PlanResult,
    PlanStatus,
)
from zekam.infrastructure.unit_test_runner.maven_runner import (
    CollectedOutputs,
    MavenRunResult,
    class_digests,
    collect_outputs,
    run_maven,
)
from zekam.infrastructure.unit_test_runner.path_safety import read_file_digest, resolve_inside
from zekam.infrastructure.unit_test_runner.pom_inspect import ProjectDiscovery
from zekam.infrastructure.unit_test_runner.surefire_report import (
    TestRunStatus,
    TestRunSummary,
    summarize,
)

_FORBIDDEN_PLUGINS = frozenset(
    {"failsafe", "exec", "antrun", "install", "deploy", "verifier", "invoker"}
)
_FORBIDDEN_GOALS = frozenset({"merge", "prepare-agent-integration", "report-integration"})
#: Kotu -> iyi degil; birden cok hedef modulde en kotu durum kazanir.
_SEVERITY_ORDER = (
    TestRunKind.INCONSISTENT_REPORT,
    TestRunKind.INTEGRATION_TESTS_PRESENT,
    TestRunKind.FLAKY_SUSPECTED,
    TestRunKind.FAILED,
    TestRunKind.NO_TESTS,
    TestRunKind.ALL_SKIPPED,
    TestRunKind.UNAVAILABLE,
    TestRunKind.PASSED,
)


@dataclass(slots=True)
class MeasurementDiagnostics:
    """Istege bagli sink: sinirli console kuyrugu ve basarisiz test id'leri (yetki degil)."""

    output_tail: str = ""
    output_bytes: int = 0
    failed_cases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanBlocked:
    """Plan hazir degil: olcum yok; setup plani/ayri izin gerekir."""

    status: PlanStatus
    stop_reason: UnitTestStopReason
    reasons: tuple[str, ...]
    setup_plan: tuple[str, ...]


def short_plugin_name(artifact_id: str) -> str:
    """``maven-surefire-plugin``/``jacoco-maven-plugin``/``surefire`` -> kisa ad."""

    name = artifact_id.removeprefix("maven-")
    for suffix in ("-maven-plugin", "-plugin"):
        name = name.removesuffix(suffix)
    return name


def _kind(status: TestRunStatus) -> TestRunKind:
    return TestRunKind(status.value)


def _worst(kinds: list[TestRunKind]) -> TestRunKind:
    for candidate in _SEVERITY_ORDER:
        if candidate in kinds:
            return candidate
    return TestRunKind.UNAVAILABLE


def _summarize_targets(
    outputs: CollectedOutputs, target_modules: tuple[str, ...]
) -> tuple[TestRunKind, TestRunSummary | None]:
    kinds: list[TestRunKind] = []
    summaries: list[TestRunSummary] = []
    for module in target_modules:
        files = outputs.surefire.get(module, ())
        try:
            summary = summarize(item.data for item in files)
        except ValidationFailed:
            kinds.append(TestRunKind.INCONSISTENT_REPORT)
            continue
        summaries.append(summary)
        kinds.append(_kind(summary.status))
    combined: TestRunSummary | None = None
    if summaries:
        combined = summarize(
            item.data for m in target_modules for item in outputs.surefire.get(m, ())
        )
    return _worst(kinds), combined


def _parse_reports(
    outputs: CollectedOutputs,
) -> tuple[dict[str, JacocoReport], set[str]]:
    reports: dict[str, JacocoReport] = {}
    broken: set[str] = set()
    for module, item in outputs.jacoco_xml.items():
        try:
            reports[module] = parse_jacoco_report(item.data)
        except ValidationFailed:
            broken.add(module)
    return reports, broken


def _observations(
    request: UnitTestRequest,
    discovery: ProjectDiscovery,
    reports: Mapping[str, JacocoReport],
    broken: set[str],
) -> tuple[CoverageObservation, ...]:
    built = build_observations(request.source_files, modules=discovery.module_dirs, reports=reports)
    if not broken:
        return built
    result: list[CoverageObservation] = []
    for item in built:
        try:
            module = resolve_java_target(item.source_file, modules=discovery.module_dirs).module
        except TargetMappingError:
            result.append(item)
            continue
        if module in broken:
            item = CoverageObservation(
                item.source_file,
                item.metric,
                CoverageState.MAPPING_ERROR,
                reason="JaCoCo raporu bozuk",
            )
        result.append(item)
    return tuple(result)


def _plugin_findings(run: MavenRunResult, expected: tuple[str, ...]) -> tuple[list[str], list[str]]:
    observed = {(short_plugin_name(a), g) for a, _v, g in run.plugin_executions}
    unexpected = sorted(
        f"{a}:{g}"
        for a, g in observed
        if a in _FORBIDDEN_PLUGINS or (a == "jacoco" and g in _FORBIDDEN_GOALS)
    )
    missing = [
        item
        for item in expected
        if (short_plugin_name(item.split(":")[0]), item.split(":")[1]) not in observed
    ]
    return unexpected, missing


def measure_unit_tests(
    request: UnitTestRequest,
    plan_result: PlanResult,
    authorization: ExecutionAuthorization | None,
    discovery: ProjectDiscovery,
    *,
    lock_dir: Path,
    run_id: str,
    attempt_id: str,
    test_candidate_digest: str,
    timeout_seconds: float,
    cancel: threading.Event | None = None,
    maven_version: str | None = None,
    environ: Mapping[str, str] | None = None,
    lock_wait_seconds: float = 0.0,
    diagnostics: MeasurementDiagnostics | None = None,
) -> MeasurementRecord | PlanBlocked:
    """Plani calistirir ve olcumu kabul/red kaydina cevirir."""

    if plan_result.status is not PlanStatus.READY or plan_result.plan is None:
        reason = {
            PlanStatus.NOT_SUPPORTED: UnitTestStopReason.TECHNOLOGY_UNSUPPORTED,
        }.get(plan_result.status, UnitTestStopReason.ENVIRONMENT_MISSING)
        return PlanBlocked(plan_result.status, reason, plan_result.reasons, plan_result.setup_plan)
    plan = plan_result.plan
    root = Path(plan.project_root)
    run = run_maven(
        plan,
        authorization,
        lock_dir=lock_dir,
        run_id=run_id,
        timeout_seconds=timeout_seconds,
        cancel=cancel,
        environ=environ,
        lock_wait_seconds=lock_wait_seconds,
    )
    if diagnostics is not None:
        diagnostics.output_tail = run.output_tail
        diagnostics.output_bytes = run.output_bytes
    empty = MeasurementEvidence(run.status, run.exit_code, TestRunKind.UNAVAILABLE, (), ())
    if run.status is not RunStatus.COMPLETED:
        return assemble_measurement(request, None, empty)

    outputs = collect_outputs(root, plan.modules, run.start_ns)
    kind, summary = _summarize_targets(outputs, plan.target_modules)
    if diagnostics is not None and summary is not None:
        diagnostics.failed_cases = tuple(
            f"{case.classname}#{case.name}"
            for suite in summary.suites
            for case in suite.cases
            if case.outcome.value in {"failed", "error"}
        )[:32]
    reports, broken = _parse_reports(outputs)
    observations = _observations(request, discovery, reports, broken)
    unexpected, missing = _plugin_findings(run, plan.expected_plugin_executions)

    production: list[tuple[str, str]] = []
    class_ids: dict[str, str] = {}
    try:
        for source in request.source_files:
            production.append((source, read_file_digest(resolve_inside(root, source))[1]))
            target = resolve_java_target(source, modules=discovery.module_dirs)
            report = reports.get(target.module)
            if report is not None:
                names = tuple(
                    c.name
                    for c in report.classes
                    if c.package == target.package and c.sourcefilename == target.name
                )
                class_ids.update(class_digests(root, target.module, names))
    except (ValidationFailed, OSError):
        return assemble_measurement(request, None, empty)

    # Taze XML'in beslendigi taze exec yoksa XML'in kaynagi dogrulanamaz (M07).
    unbound_xml = tuple(
        f"jacoco-exec yok, xml bagsiz: {m or '.'}"
        for m in outputs.jacoco_xml
        if m not in outputs.exec_digests
    )
    report_digests: list[tuple[str, str]] = [("process-output", run.output_digest)]
    for module, files in outputs.surefire.items():
        report_digests += [(f"surefire:{f.relative}", f.digest) for f in files]
        if module in outputs.jacoco_xml:
            report_digests.append(
                (f"jacoco-xml:{module or '.'}", outputs.jacoco_xml[module].digest)
            )
    report_digests += [(f"jacoco-exec:{m or '.'}", d) for m, d in outputs.exec_digests.items()]

    jvm = dict(summary.jvm) if summary else {}
    plugin_versions = {short_plugin_name(a): v for a, v, _g in run.plugin_executions}
    release = next(
        (m.compile_release for m in discovery.modules if m.module in plan.target_modules), None
    )
    toolchain = ToolchainIdentity(
        launcher_kind=plan.launcher.kind.value,
        launcher_digest=plan.launcher.digest,
        maven_version=maven_version,
        test_jvm_version=jvm.get("java.version"),
        compile_release=release,
        plugin_versions=tuple(plugin_versions.items()),
    )
    binding = MeasurementBinding(
        request_digest=request.request_digest,
        attempt_id=attempt_id,
        run_id=run_id,
        scope=MeasurementScope.UNIT,
        source_revision=request.source_revision,
        production_digests=tuple(production),
        class_digests=tuple(class_ids.items()),
        test_candidate_digest=test_candidate_digest,
        build_config_digest=plan.config_digest,
        plan_digest=plan.plan_digest,
        toolchain=toolchain,
        report_digests=tuple(report_digests),
    )
    counts = (
        ()
        if summary is None
        else (
            ("discovered", summary.discovered),
            ("executed", summary.executed),
            ("passed", summary.passed),
            ("failed", summary.failed),
            ("errors", summary.errors),
            ("skipped", summary.skipped),
            ("aborted", summary.aborted),
        )
    )
    evidence = MeasurementEvidence(
        run_status=run.status,
        exit_code=run.exit_code,
        test_kind=kind,
        test_counts=counts,
        observations=observations,
        integration_artifacts_changed=outputs.integration_changed,
        unexpected_plugin_executions=tuple(unexpected),
        missing_expected_plugins=tuple(missing),
        stale_report_paths=(*outputs.stale, *unbound_xml),
    )
    return assemble_measurement(request, binding, evidence, freshness=FreshnessVerdict.FRESH)


def digest_files(root: Path, relatives: tuple[str, ...]) -> dict[str, str]:
    """Guncel production digest'lerini ``CurrentState`` icin hesaplar (M12)."""

    return {
        relative: digest_of_bytes(read_file_digest(resolve_inside(root, relative))[0])
        for relative in relatives
    }
