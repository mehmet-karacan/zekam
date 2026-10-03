"""Unit-test dongusunun olcum ve ortam portlari icin gercek Maven/JaCoCo adaptorleri (W05).

``MavenUnitTestMeasurer`` her olcumde plani yeniden kurar (config drift gorunur kalsin) ve
``measure_unit_tests`` ile GERCEK sureci calistirir; basarisiz test id'leri ve sinirli
console kuyrugu yalniz neden siniflandirmasi icin ``MeasuredRun``'a tasinir.
``MavenEnvironmentObserver`` guncel production digest'lerini, build config ve launcher
kimligini okur; salt okunurdur (build calistirmaz).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from zekam.application.unit_test_loop_state import EnvironmentObservation, MeasuredRun
from zekam.domain.errors import ValidationFailed
from zekam.domain.unit_test_engineering import UnitTestRequest, UnitTestStopReason
from zekam.infrastructure.unit_test_runner.maven_plan import (
    ExecutionAuthorization,
    PlanStatus,
    build_unit_test_plan,
)
from zekam.infrastructure.unit_test_runner.measurement import (
    MeasurementDiagnostics,
    PlanBlocked,
    digest_files,
    measure_unit_tests,
)
from zekam.infrastructure.unit_test_runner.pom_inspect import discover_project


@dataclass(frozen=True, slots=True)
class MavenUnitTestMeasurer:
    project_root: Path
    target_modules: tuple[str, ...]
    lock_dir: Path
    allow_network: bool = False
    search_path: str | None = None
    maven_version: str | None = None
    #: Verilirse yalniz bu exact plan digest'i calisir (yetki baska plana tasinmaz).
    approved_plan_digest: str | None = None
    environ: Mapping[str, str] | None = None

    def measure(
        self,
        request: UnitTestRequest,
        *,
        attempt_id: str,
        run_id: str,
        candidate_digest: str,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> MeasuredRun:
        plan_result = build_unit_test_plan(
            self.project_root, target_modules=self.target_modules, search_path=self.search_path
        )
        authorization: ExecutionAuthorization | None = None
        if plan_result.status is PlanStatus.READY and plan_result.plan is not None:
            plan = plan_result.plan
            if (
                self.approved_plan_digest is not None
                and plan.plan_digest != self.approved_plan_digest
            ):
                return MeasuredRun(
                    None,
                    UnitTestStopReason.ENVIRONMENT_MISSING,
                    ("plan-digest-drift: yetki baska plana ait",),
                )
            authorization = ExecutionAuthorization(
                plan.plan_digest, plan.execution_class, allow_network=self.allow_network
            )
        if authorization is None:
            reason = (
                UnitTestStopReason.TECHNOLOGY_UNSUPPORTED
                if plan_result.status is PlanStatus.NOT_SUPPORTED
                else UnitTestStopReason.ENVIRONMENT_MISSING
            )
            return MeasuredRun(None, reason, (*plan_result.reasons, *plan_result.setup_plan))
        diagnostics = MeasurementDiagnostics()
        result = measure_unit_tests(
            request,
            plan_result,
            authorization,
            discover_project(self.project_root),
            lock_dir=self.lock_dir,
            run_id=run_id,
            attempt_id=attempt_id,
            test_candidate_digest=candidate_digest,
            timeout_seconds=timeout_seconds,
            cancel=cancel,
            maven_version=self.maven_version,
            environ=self.environ,
            diagnostics=diagnostics,
        )
        if isinstance(result, PlanBlocked):
            return MeasuredRun(None, result.stop_reason, result.reasons)
        return MeasuredRun(
            result,
            None,
            (),
            diagnostics.failed_cases,
            diagnostics.output_tail,
            diagnostics.output_bytes,
        )


@dataclass(frozen=True, slots=True)
class MavenEnvironmentObserver:
    project_root: Path
    target_modules: tuple[str, ...]
    source_files: tuple[str, ...]
    revision_provider: Callable[[], str]
    dirty_provider: Callable[[], frozenset[str]] = field(default=lambda: frozenset())
    search_path: str | None = None

    def observe(self) -> EnvironmentObservation:
        plan_result = build_unit_test_plan(
            self.project_root, target_modules=self.target_modules, search_path=self.search_path
        )
        if plan_result.plan is None:
            raise ValidationFailed("Ortam gozlemi icin hazir Maven plani yok")
        plan = plan_result.plan
        return EnvironmentObservation(
            source_revision=self.revision_provider(),
            production_digests=digest_files(self.project_root, self.source_files),
            build_config_digest=plan.config_digest,
            launcher_digest=plan.launcher.digest,
            protected_dirty_paths=self.dirty_provider(),
        )
