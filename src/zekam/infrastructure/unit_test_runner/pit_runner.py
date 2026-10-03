"""PIT calistirici: PIT yetkisi -> Maven runner -> taze ``mutations.xml`` -> ``MutationOutcome``.

- Yalniz ``pit_plan.PitPlan`` calisir; ``PitAuthorization`` exact plan digest'ine ve ayri
  ``bytecode-mutation`` etkisine bagli olmalidir. Ic Maven yetkisi ancak bundan turetilir.
- Onceki ``target/pit-reports`` dizini build kilidi altinda ``*.stale-*`` adina tasinir
  (silinmez); kosudan sonra yalniz yeni olusan rapor kabul edilir.
- Surec basarisiz/timeout/iptal, exit code != 0, pitest plugin kaydinin gorulmemesi, eksik
  veya gecersiz rapor ``COMPLETED`` sayilmaz; kismi skor uretilmez.
- Kaynak Java dosyasina hicbir sey yazilmaz; PIT bytecode'u kendi isleminde isler.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from zekam.application.unit_test_measurement import RunStatus
from zekam.application.unit_test_mutation import MutationOutcome, MutationRunState
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.unit_test_runner.maven_runner import (
    MavenRunResult,
    build_lock,
    run_maven,
)
from zekam.infrastructure.unit_test_runner.path_safety import resolve_inside
from zekam.infrastructure.unit_test_runner.pit_plan import (
    PIT_ARTIFACT,
    PitAuthorization,
    PitPlan,
    compute_test_candidate_digest,
    maven_authorization_for,
)
from zekam.infrastructure.unit_test_runner.pit_report import parse_pit_report, read_pit_report

_MTIME_TOLERANCE_NS: Final = 2_000_000_000


@dataclass(frozen=True, slots=True)
class PitRunResult:
    maven: MavenRunResult | None
    outcome: MutationOutcome


def _pit_dir(plan: PitPlan) -> str:
    module = plan.approval.module
    return f"{module}/target/pit-reports" if module else "target/pit-reports"


def _quarantine_reports(root: Path, plan: PitPlan, run_id: str) -> None:
    relative = _pit_dir(plan)
    path = resolve_inside(root, relative)
    if not path.exists():
        return
    suffix = f".stale-{hashlib.sha256(run_id.encode('utf-8')).hexdigest()[:12]}"
    destination = path.with_name(path.name + suffix)
    counter = 0
    while destination.exists():
        counter += 1
        destination = path.with_name(f"{path.name}{suffix}-{counter}")
    try:
        path.rename(destination)
    except OSError as exc:
        raise PolicyViolation("Eski PIT raporu karantinaya alinamadi; kosu reddedildi") from exc


def _failed(
    plan: PitPlan, state: MutationRunState, *reasons: str, maven: MavenRunResult | None = None
) -> PitRunResult:
    return PitRunResult(
        maven, MutationOutcome(state, plan_digest=plan.pit_plan_digest, reasons=reasons)
    )


def run_pit(
    plan: PitPlan,
    authorization: PitAuthorization | None,
    *,
    lock_dir: Path,
    run_id: str,
    cancel: threading.Event | None = None,
    environ: Mapping[str, str] | None = None,
) -> PitRunResult:
    inner = maven_authorization_for(plan, authorization)
    root = Path(plan.maven_plan.project_root)
    if compute_test_candidate_digest(root, plan.approval.module) != plan.test_candidate_digest:
        raise PolicyViolation("Test adayi plandan farkli (aday drift); yeniden plan gerekir")
    with build_lock(lock_dir, root):
        _quarantine_reports(root, plan, run_id)
    result = run_maven(
        plan.maven_plan,
        inner,
        lock_dir=lock_dir,
        run_id=run_id,
        timeout_seconds=float(plan.approval.process_timeout_seconds),
        cancel=cancel,
        environ=environ,
        quarantine=False,
    )
    if result.status is RunStatus.TOOL_MISSING:
        return _failed(plan, MutationRunState.TOOL_MISSING, "launcher bulunamadi", maven=result)
    if result.status is not RunStatus.COMPLETED:
        return _failed(
            plan, MutationRunState.RUN_FAILED, f"run: {result.status.value}", maven=result
        )
    if result.exit_code != 0:
        return _failed(
            plan, MutationRunState.RUN_FAILED, f"build exit code {result.exit_code}", maven=result
        )
    if not any(item[0] == PIT_ARTIFACT for item in result.plugin_executions):
        return _failed(
            plan, MutationRunState.RUN_FAILED, "pitest plugin calismasi gorulmedi", maven=result
        )
    if compute_test_candidate_digest(root, plan.approval.module) != plan.test_candidate_digest:
        return _failed(
            plan, MutationRunState.RUN_FAILED, "kosu sirasinda test aday drift'i", maven=result
        )
    return _collect(plan, root, result)


def _collect(plan: PitPlan, root: Path, result: MavenRunResult) -> PitRunResult:
    try:
        path = resolve_inside(root, plan.report_path)
        if path.is_file() and path.stat().st_mtime_ns < result.start_ns - _MTIME_TOLERANCE_NS:
            return _failed(plan, MutationRunState.REPORT_INVALID, "rapor bayat", maven=result)
        data = read_pit_report(root, plan.report_path)
        report = parse_pit_report(data)
    except ValidationFailed as exc:
        return _failed(plan, MutationRunState.REPORT_INVALID, str(exc), maven=result)
    outcome = MutationOutcome(
        MutationRunState.COMPLETED,
        plan_digest=plan.pit_plan_digest,
        report=report,
        report_digest=digest_of_bytes(data),
        candidate_digest=plan.test_candidate_digest,
    )
    return PitRunResult(result, outcome)
