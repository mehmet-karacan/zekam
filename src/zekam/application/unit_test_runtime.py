"""Unit-test icin yerel production composition root (W07) ve native yardimci olcum yolu (W05).

Iki ayri bilesim vardir ve birbirine bagimli degildir:

- Maven olcum/ortam/ledger bilesenleri (``compose_maven_measurement``): OpenCode veya baska
  bir model gerektirmez; native ajan testi yazar, Zekam gercek surecle olcer ve kaydeder.
- Agent gateway (``compose_agent_gateway``): yalniz acikca secilen eski otomatik OpenCode batch
  yolu (``compose_unit_test_runtime``) icin kurulur; executable onkosulu yalniz burada aranir.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.application.object_store import ObjectStore
from zekam.application.unit_test_agents import (
    AgentSpecialty,
    CanonicalUnitTestAgentGateway,
    UnitTestAgentGateway,
    WorkBinding,
)
from zekam.application.unit_test_ledger import UnitTestLedger
from zekam.application.unit_test_loop import UnitTestLoopService
from zekam.application.unit_test_loop_state import (
    EnvironmentObservation,
    EnvironmentObserver,
    LoopControl,
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
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import (
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
    ZekamError,
)
from zekam.domain.unit_test_engineering import (
    AttemptState,
    CoverageObservation,
    EvaluationVerdict,
    UnitTestAttempt,
    UnitTestRequest,
    UnitTestStopReason,
    UnitTestTerminal,
)
from zekam.infrastructure.clients.adapters import opencode_adapter
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_records import CasAssignmentStore, CasRecordIndex
from zekam.infrastructure.unit_test_runner.loop_adapters import (
    MavenEnvironmentObserver,
    MavenUnitTestMeasurer,
)
from zekam.infrastructure.unit_test_runner.patch_workspace import FileSystemTestPatchWorkspace
from zekam.infrastructure.unit_test_runner.pom_inspect import discover_project
from zekam.infrastructure.unit_test_runner.tree_fingerprint import (
    digest_test_tree,
    fingerprint_tree,
)

ArtifactRegistrar = Callable[[str, int, str], None]


@dataclass(frozen=True, slots=True)
class UnitTestRuntimeBinding:
    """Loop'un uyduramayacagi Work/plan/source baglari."""

    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    coordinator_assignment_id: UUID
    project_root: Path
    object_store_root: Path
    lock_dir: Path
    opencode_executable: Path
    approved_maven_plan_digest: str
    target_modules: tuple[str, ...] = ()
    search_path: str | None = None
    allow_network: bool = False
    remote_model: bool = False
    model_id: str | None = None
    plan_id: UUID | None = None
    step_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("project_root", "object_store_root", "lock_dir", "opencode_executable"):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ConfigurationError(f"{name} absolute Path olmali")
        if not self.approved_maven_plan_digest.startswith("sha256:"):
            raise ValidationFailed("approved_maven_plan_digest sha256 olmali")
        if not self.opencode_executable.is_file():
            raise ConfigurationError("OpenCode executable exact dosya olmali")
        if self.allow_network and not self.remote_model:
            raise ValidationFailed("network yalniz explicit remote model baglaminda acilabilir")
        if self.model_id is not None and not self.model_id.strip():
            raise ValidationFailed("model_id bos olamaz")

    def maven_binding(self) -> MavenMeasurementBinding:
        """Agent'tan bagimsiz Maven olcum bagi (executable/model alanlari tasimaz)."""

        return MavenMeasurementBinding(
            project_root=self.project_root,
            object_store_root=self.object_store_root,
            lock_dir=self.lock_dir,
            approved_maven_plan_digest=self.approved_maven_plan_digest,
            target_modules=self.target_modules,
            search_path=self.search_path,
            allow_network=self.allow_network,
        )


@dataclass(frozen=True, slots=True)
class MavenMeasurementBinding:
    """Native yardimci yolun exact olcum bagi: OpenCode/model/work-agent alani YOKTUR."""

    project_root: Path
    object_store_root: Path
    lock_dir: Path
    approved_maven_plan_digest: str
    target_modules: tuple[str, ...] = ()
    search_path: str | None = None
    allow_network: bool = False

    def __post_init__(self) -> None:
        for name in ("project_root", "object_store_root", "lock_dir"):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ConfigurationError(f"{name} absolute Path olmali")
        if not self.project_root.is_dir():
            raise ConfigurationError("project_root exact dizin olmali")
        if not self.approved_maven_plan_digest.startswith("sha256:"):
            raise ValidationFailed("approved_maven_plan_digest sha256 olmali")


@dataclass(frozen=True, slots=True)
class UnitTestRuntime:
    """Composition sonucu; caller transaction/commit sinirini yonetir."""

    loop: UnitTestLoopService
    objects: ObjectStore
    gateway: UnitTestAgentGateway


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or len(revision) != 40:
        raise ValidationFailed("git source revision okunamadi")
    return revision


def _git_dirty(root: Path) -> frozenset[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 2_000_000:
        raise ValidationFailed("git dirty durumu okunamadi")
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().replace("\\", "/")
        if path:
            paths.add(path)
    return frozenset(paths)


@dataclass(frozen=True, slots=True)
class MavenMeasurementParts:
    """Maven olcum, ortam gozlemi ve test calisma alani; agent gateway icermez."""

    objects: ObjectStore
    measurer: UnitTestMeasurer
    environment: EnvironmentObserver
    workspace: FileSystemTestPatchWorkspace


def compose_maven_measurement(
    request: UnitTestRequest, binding: MavenMeasurementBinding
) -> MavenMeasurementParts:
    """Gercek Maven/JaCoCo olcer + ortam gozlemcisi + CAS; OpenCode aranmaz."""

    objects = LocalContentAddressedStore(binding.object_store_root).ensure()
    measurer = MavenUnitTestMeasurer(
        project_root=binding.project_root,
        target_modules=binding.target_modules,
        lock_dir=binding.lock_dir,
        allow_network=binding.allow_network,
        search_path=binding.search_path,
        approved_plan_digest=binding.approved_maven_plan_digest,
    )
    environment = MavenEnvironmentObserver(
        project_root=binding.project_root,
        target_modules=binding.target_modules,
        source_files=request.source_files,
        revision_provider=lambda: _git_revision(binding.project_root),
        dirty_provider=lambda: _git_dirty(binding.project_root),
        search_path=binding.search_path,
    )
    return MavenMeasurementParts(
        objects=objects,
        measurer=measurer,
        environment=environment,
        workspace=FileSystemTestPatchWorkspace(binding.project_root),
    )


def compose_agent_gateway(
    binding: UnitTestRuntimeBinding,
    *,
    request: UnitTestRequest,
    index_objects: ObjectStore,
    artifact_registrar: ArtifactRegistrar,
) -> UnitTestAgentGateway:
    """Yalniz acik OpenCode batch yolu icin agent gateway; native yol bunu cagirmaz."""

    index = CasRecordIndex(index_objects)
    assignments = CasAssignmentStore(
        index,
        request.request_digest,
        registrar=artifact_registrar,
    )
    adapter = opencode_adapter(str(binding.opencode_executable), model_id=binding.model_id)
    adapters = dict.fromkeys(AgentSpecialty, adapter)
    return CanonicalUnitTestAgentGateway(
        work=WorkBinding(
            realm_id=binding.realm_id,
            project_id=binding.project_id,
            work_item_id=binding.work_item_id,
            coordinator_assignment_id=binding.coordinator_assignment_id,
            plan_id=binding.plan_id,
            step_id=binding.step_id,
        ),
        store=assignments,
        objects=index_objects,
        adapters=adapters,
        cwd=binding.project_root,
        is_remote=binding.remote_model,
    )


def compose_unit_test_runtime(
    request: UnitTestRequest,
    *,
    ledger: UnitTestLedger,
    binding: UnitTestRuntimeBinding,
    artifact_registrar: ArtifactRegistrar,
    control: LoopControl | None = None,
) -> UnitTestRuntime:
    """Mevcut operational UoW ve exact binding ile gercek loop adaptorlerini baglar.

    Eski explicit OpenCode batch yolu: davranis degismez; Maven bilesenleri ve agent gateway
    artik ayri kurulur.
    """

    parts = compose_maven_measurement(request, binding.maven_binding())
    gateway = compose_agent_gateway(
        binding,
        request=request,
        index_objects=parts.objects,
        artifact_registrar=artifact_registrar,
    )
    loop = UnitTestLoopService(
        ledger=ledger,
        objects=parts.objects,
        gateway=gateway,
        measurer=parts.measurer,
        workspace=parts.workspace,
        environment=parts.environment,
        control=control,
    )
    return UnitTestRuntime(loop=loop, objects=parts.objects, gateway=gateway)


# -- native yardimci olcum yolu (W05) ----------------------------------------------------

#: Native istek, batch/loop isteginden ayri digest tasir: ayni ledger'da loop resume'u ile
#: karismaz (loop kanit nesnesi ``state`` bekler, native kanit nesnesi beklemez).
NATIVE_MODE_MARKER = "measurement-mode=native-helper"
NATIVE_EVIDENCE_CONTRACT = "zekam-unit-test-native-evidence/v1"
NATIVE_CANDIDATE_CONTRACT = "zekam-unit-test-native-candidate/v1"


def native_request(request: UnitTestRequest) -> UnitTestRequest:
    """Istegi native yardimci moda isaretler (idempotent)."""

    if NATIVE_MODE_MARKER in request.defaults_applied:
        return request
    return replace(request, defaults_applied=(*request.defaults_applied, NATIVE_MODE_MARKER))


def is_native_request(request: UnitTestRequest) -> bool:
    return NATIVE_MODE_MARKER in request.defaults_applied


class NativeOutcome(StrEnum):
    """Native olcumun makine-okunur sonucu; ``success`` boolean'i yoktur."""

    TARGET_MET = "target-met"
    BELOW_TARGET = "below-target"
    NO_TESTS = "no-tests"
    TESTS_FAILED = "tests-failed"
    SOURCE_DRIFT = "source-drift"
    STALE_OR_UNBOUND = "stale-or-unbound"
    INCOMPLETE = "measurement-incomplete"
    ENVIRONMENT_MISSING = "environment-missing"
    TECHNOLOGY_UNSUPPORTED = "technology-unsupported"
    WRONG_TARGET = "wrong-target"
    BUDGET_EXHAUSTED = "budget-exhausted"
    ALREADY_FINAL = "already-final"


#: Kararli cikis kodlari (``unit_test_outcome`` tablosuyla cakismaz; 0 yalniz taze hedef gozlemi).
NATIVE_EXIT_CODES: Mapping[NativeOutcome, int] = {
    NativeOutcome.TARGET_MET: 0,
    NativeOutcome.BUDGET_EXHAUSTED: 20,
    NativeOutcome.BELOW_TARGET: 22,
    NativeOutcome.NO_TESTS: 23,
    NativeOutcome.TESTS_FAILED: 24,
    NativeOutcome.SOURCE_DRIFT: 25,
    NativeOutcome.STALE_OR_UNBOUND: 26,
    NativeOutcome.WRONG_TARGET: 27,
    NativeOutcome.ENVIRONMENT_MISSING: 30,
    NativeOutcome.TECHNOLOGY_UNSUPPORTED: 31,
    NativeOutcome.INCOMPLETE: 33,
    NativeOutcome.ALREADY_FINAL: 77,
}


@dataclass(frozen=True, slots=True)
class NativeMeasurement:
    """Tek native olcum denemesinin sonucu (ledger receipt'i ile birlikte)."""

    request_digest: str
    outcome: NativeOutcome
    attempt_id: str | None = None
    ordinal: int | None = None
    attempt_state: AttemptState | None = None
    evidence_digest: str | None = None
    record_digest: str | None = None
    measurement_status: str | None = None
    exit_code: int | None = None
    test_counts: tuple[tuple[str, int], ...] = ()
    observations: tuple[Mapping[str, Any], ...] = ()
    aggregate: tuple[int, int] | None = None
    blockers: tuple[str, ...] = ()
    failed_cases: tuple[str, ...] = ()
    freshness: str | None = None
    drift: tuple[str, ...] = ()
    remaining_attempts: int = 0
    next_safe_action: str = ""
    receipt_recorded: bool = False
    detail: tuple[str, ...] = field(default_factory=tuple)

    @property
    def target_met(self) -> bool:
        return self.outcome is NativeOutcome.TARGET_MET

    @property
    def process_exit_code(self) -> int:
        return NATIVE_EXIT_CODES[self.outcome]

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": "zekam-unit-test-measure/v1",
            "request_digest": self.request_digest,
            "outcome": self.outcome.value,
            "exit_code": self.process_exit_code,
            "target_met_observed": self.target_met,
            "terminal_recorded": False,
            "quality_verification": "ratio-observed-only",
            "attempt_id": self.attempt_id,
            "ordinal": self.ordinal,
            "attempt_state": None if self.attempt_state is None else self.attempt_state.value,
            "receipt_recorded": self.receipt_recorded,
            "evidence_digest": self.evidence_digest,
            "measurement_record_digest": self.record_digest,
            "measurement_status": self.measurement_status,
            "build_exit_code": self.exit_code,
            "test_counts": dict(self.test_counts),
            "observations": [dict(item) for item in self.observations],
            "aggregate": None if self.aggregate is None else list(self.aggregate),
            "failed_cases": list(self.failed_cases),
            "blockers": list(self.blockers),
            "freshness": self.freshness,
            "drift": list(self.drift),
            "remaining_attempts": self.remaining_attempts,
            "next_safe_action": self.next_safe_action,
            "detail": list(self.detail),
            "model_calls": 0,
            "opencode_required": False,
            "provider_calls": 0,
        }


def _next_action(outcome: NativeOutcome, remaining: int, failed: tuple[str, ...]) -> str:
    again = f"kalan olcum hakki: {remaining}" if remaining > 0 else "olcum butcesi bitti"
    failed_text = f": {', '.join(failed[:5])}" if failed else ""
    return {
        NativeOutcome.TARGET_MET: (
            "Hedef taze olcumle gozlendi; kalite/bagimsiz dogrulama ve Work kapanisi ayrica "
            "gerekir. `zekam test evidence` ile kaniti okuyun."
        ),
        NativeOutcome.BELOW_TARGET: (
            "Eksik satir/dallari kanittan analiz edin, yalniz izinli test yollarina anlamli "
            "testler ekleyin, sonra ayni plan digest'iyle `zekam test measure` calistirin "
            f"({again})."
        ),
        NativeOutcome.NO_TESTS: (
            "Hedef icin calisan test bulunamadi; coverage uretilmedi. Test yazin, sonra "
            f"`zekam test measure` calistirin ({again})."
        ),
        NativeOutcome.TESTS_FAILED: (
            "Basarisiz testleri duzeltin (assertion gevsetmeyin, exclusion eklemeyin)"
            f"{failed_text}; sonra tekrar olcun ({again})."
        ),
        NativeOutcome.SOURCE_DRIFT: (
            "Olcum sirasinda veya oncesinde kaynak/test/config degisti; sonuc kabul edilmedi. "
            f"Degisiklikleri durdurup commit/revision'i sabitleyin ve tekrar olcun ({again})."
        ),
        NativeOutcome.STALE_OR_UNBOUND: (
            "Rapor taze/bagli degil (eski rapor, eksik exec veya karisik kapsam); coverage "
            f"kabul edilmedi. Calisma agacini sabitleyip tekrar olcun ({again})."
        ),
        NativeOutcome.INCOMPLETE: (
            "Olcum tamamlanmadi (timeout/iptal/eksik plugin/exit status). Nedeni inceleyin; "
            f"sonucu basari saymayin ({again})."
        ),
        NativeOutcome.ENVIRONMENT_MISSING: (
            "Maven/JaCoCo ortami hazir degil; `zekam test plan --project-root ...` setup "
            "planini izleyin. Sessiz POM degisikligi yapilmaz."
        ),
        NativeOutcome.TECHNOLOGY_UNSUPPORTED: (
            "Proje Maven/JUnit unit-test olcumu icin desteklenmiyor; baska arac sessizce secilmez."
        ),
        NativeOutcome.WRONG_TARGET: (
            "Hedef kaynak dosyasi okunamadi veya modulle eslesmedi; exact proje-relative Java "
            "yolunu duzeltip yeni plan uretin."
        ),
        NativeOutcome.BUDGET_EXHAUSTED: (
            "Onayli olcum butcesi (max-attempts) bitti; yeni olcum icin kullanicidan yeni "
            "exact butce/plan onayi isteyin."
        ),
        NativeOutcome.ALREADY_FINAL: "Istek final terminal ile kapali; yeni plan acin.",
    }[outcome]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class NativeMeasurementPorts:
    """Native olcumun I/O baglari (gercek Maven bilesenleri veya test icin saf fake)."""

    ledger: UnitTestLedger
    objects: ObjectStore
    measurer: UnitTestMeasurer
    environment: EnvironmentObserver
    test_tree: Callable[[], Mapping[str, str]]
    plan_digest: str
    artifact_registrar: ArtifactRegistrar | None = None
    clock: Callable[[], dt.datetime] = _utc_now


def _put(ports: NativeMeasurementPorts, payload: Mapping[str, Any]) -> str:
    data = canonical_bytes(payload)
    info = ports.objects.put(data, media_type="application/json")
    if ports.artifact_registrar is not None:
        ports.artifact_registrar(info.digest, len(data), "application/json")
    return info.digest


def _close_attempt(
    ports: NativeMeasurementPorts,
    attempt: UnitTestAttempt,
    state: AttemptState,
    evidence: Mapping[str, Any],
    observations: tuple[Any, ...] = (),
) -> str:
    evidence_digest = _put(
        ports,
        {
            "contract": NATIVE_EVIDENCE_CONTRACT,
            "request_digest": attempt.request_digest,
            "attempt_id": attempt.attempt_id,
            "ordinal": attempt.ordinal,
            "status": state.value,
            "evidence": dict(evidence),
        },
    )
    ports.ledger.record_attempt_receipt(
        attempt_id=attempt.attempt_id,
        status=state,
        evidence_digest=evidence_digest,
        observations=observations,
        now=ports.clock(),
    )
    return evidence_digest


def _recover_unreceipted(ports: NativeMeasurementPorts, request_digest: str) -> tuple[str, ...]:
    """Cokmus onceki native claim'i INTERRUPTED kapatir; isi yeniden calistirmaz."""

    recovered: list[str] = []
    attempts = {a.attempt_id: a for a in ports.ledger.list_attempts(request_digest)}
    for attempt_id in ports.ledger.unreceipted_attempts(request_digest):
        attempt = attempts[attempt_id]
        _close_attempt(
            ports, attempt, AttemptState.INTERRUPTED, {"recovered": "native-claim-without-receipt"}
        )
        recovered.append(attempt_id)
    return tuple(recovered)


def _blocked(
    request: UnitTestRequest,
    outcome: NativeOutcome,
    *,
    remaining: int,
    blockers: tuple[str, ...] = (),
    detail: tuple[str, ...] = (),
) -> NativeMeasurement:
    return NativeMeasurement(
        request.request_digest,
        outcome,
        blockers=blockers,
        remaining_attempts=remaining,
        next_safe_action=_next_action(outcome, remaining, ()),
        detail=detail,
    )


def _freshness(
    record: MeasurementRecord,
    after: EnvironmentObservation,
    request: UnitTestRequest,
    candidate_digest: str,
) -> FreshnessVerdict | None:
    binding = record.binding
    if binding is None:
        return None
    toolchain = (
        binding.toolchain.toolchain_digest
        if after.launcher_digest == binding.toolchain.launcher_digest
        else digest("launcher-drift")
    )
    return check_freshness(
        binding,
        CurrentState(
            request_digest=request.request_digest,
            source_revision=after.source_revision,
            production_digests=after.production_digests,
            build_config_digest=after.build_config_digest,
            toolchain_digest=toolchain,
            test_candidate_digest=candidate_digest,
        ),
    )


def _classify(
    record: MeasurementRecord, *, drift: tuple[str, ...], verdict: FreshnessVerdict | None
) -> NativeOutcome:
    """Kabul karari: drift/stale her zaman coverage'i reddeder; exit 0 tek basina sayilmaz."""

    if drift:
        return NativeOutcome.SOURCE_DRIFT
    if verdict is not None and verdict is not FreshnessVerdict.FRESH:
        return (
            NativeOutcome.SOURCE_DRIFT
            if verdict.is_external_drift
            else NativeOutcome.STALE_OR_UNBOUND
        )
    status = record.status
    if status is MeasurementStatus.ACCEPTED:
        evaluation = record.evaluation
        if evaluation is None:
            return NativeOutcome.INCOMPLETE
        if evaluation.verdict is EvaluationVerdict.MET:
            return NativeOutcome.TARGET_MET
        if evaluation.verdict is EvaluationVerdict.NOT_MET:
            return NativeOutcome.BELOW_TARGET
        return NativeOutcome.INCOMPLETE
    return {
        MeasurementStatus.NO_TESTS_BASELINE: NativeOutcome.NO_TESTS,
        MeasurementStatus.TESTS_FAILED: NativeOutcome.TESTS_FAILED,
        MeasurementStatus.UNBOUND_OR_STALE: NativeOutcome.STALE_OR_UNBOUND,
        MeasurementStatus.TOOL_MISSING: NativeOutcome.ENVIRONMENT_MISSING,
    }.get(status, NativeOutcome.INCOMPLETE)


def measure_native(request: UnitTestRequest, ports: NativeMeasurementPorts) -> NativeMeasurement:
    """Claim-before-effect: gercek Maven olcumu + taze rapor/digest/drift dogrulamasi + receipt.

    Model veya OpenCode cagrilmaz. Terminal (``target-reached``) yazilmaz; yalniz attempt +
    receipt kaydi ve makine-okunur sonuc uretilir. Her cagri butce icinde yeni bir
    kontrollu tekrar olcumdur.
    """

    if not is_native_request(request):
        raise ValidationFailed("Native olcum yalniz native-helper isaretli istekle calisir")
    request_digest = request.request_digest
    ledger = ports.ledger
    ledger.register_request(request, now=ports.clock())
    terminals = ledger.list_terminals(request_digest)
    if terminals and terminals[-1].final:
        return _blocked(request, NativeOutcome.ALREADY_FINAL, remaining=0)
    recovered = _recover_unreceipted(ports, request_digest)
    attempts = ledger.list_attempts(request_digest)
    max_attempts = request.budget.max_attempts
    ordinal = len(attempts) + 1
    remaining_after = max(0, max_attempts - ordinal)
    if ordinal > max_attempts:
        return _blocked(request, NativeOutcome.BUDGET_EXHAUSTED, remaining=0, detail=recovered)

    # Effect oncesi dogrulamalar: burada hata claim olusturmaz.
    try:
        before = ports.environment.observe()
        tree_before = dict(ports.test_tree())
    except (ValidationFailed, PolicyViolation, OSError) as exc:
        text = str(exc)
        wrong_target = "Dosya yok" in text or "Dosya boyut" in text or "Yol " in text
        return _blocked(
            request,
            NativeOutcome.WRONG_TARGET if wrong_target else NativeOutcome.ENVIRONMENT_MISSING,
            remaining=remaining_after + 1,
            blockers=(text[:200],),
            detail=recovered,
        )
    if before.source_revision != request.source_revision:
        return _blocked(
            request,
            NativeOutcome.SOURCE_DRIFT,
            remaining=remaining_after + 1,
            blockers=("source-revision",),
            detail=recovered,
        )
    short = request_digest.removeprefix("sha256:")[:16]
    candidate_digest = _put(
        ports,
        {
            "contract": NATIVE_CANDIDATE_CONTRACT,
            "kind": "native-test-tree",
            "test_tree_digest": fingerprint_tree(tree_before),
            "files": sorted(tree_before.items()),
        },
    )
    attempt = UnitTestAttempt(
        attempt_id=f"ut-{short}-n{ordinal}",
        request_digest=request_digest,
        ordinal=ordinal,
        parent_attempt_id=attempts[-1].attempt_id if attempts else None,
        plan_digest=ports.plan_digest,
        candidate_digest=candidate_digest,
        idempotency_key=f"ut-{short}-n{ordinal}-claim",
    )
    try:
        ledger.claim_attempt(attempt, now=ports.clock())
    except PolicyViolation as exc:
        return _blocked(
            request,
            NativeOutcome.BUDGET_EXHAUSTED,
            remaining=0,
            blockers=(str(exc)[:200],),
            detail=recovered,
        )

    budget = request.budget
    timeout = float(min(budget.process_timeout_seconds, budget.total_elapsed_seconds))
    try:
        run: MeasuredRun = ports.measurer.measure(
            request,
            attempt_id=attempt.attempt_id,
            run_id=f"{attempt.attempt_id}-run",
            candidate_digest=candidate_digest,
            timeout_seconds=timeout,
            cancel=threading.Event(),
        )
    except Exception as exc:
        evidence_digest = _close_attempt(
            ports, attempt, AttemptState.INTERRUPTED, {"error": type(exc).__name__}
        )
        return NativeMeasurement(
            request_digest,
            NativeOutcome.ENVIRONMENT_MISSING,
            attempt.attempt_id,
            ordinal,
            AttemptState.INTERRUPTED,
            evidence_digest,
            blockers=(
                f"measurer-error:{type(exc).__name__}"
                + (f": {str(exc)[:160]}" if isinstance(exc, ZekamError) else ""),
            ),
            remaining_attempts=remaining_after,
            next_safe_action=_next_action(NativeOutcome.ENVIRONMENT_MISSING, remaining_after, ()),
            receipt_recorded=True,
            detail=recovered,
        )

    if run.record is None:
        outcome = (
            NativeOutcome.TECHNOLOGY_UNSUPPORTED
            if run.blocked_reason is UnitTestStopReason.TECHNOLOGY_UNSUPPORTED
            else NativeOutcome.ENVIRONMENT_MISSING
        )
        evidence_digest = _close_attempt(
            ports, attempt, AttemptState.FAILED, {"blocked": list(run.blocked_details)}
        )
        return NativeMeasurement(
            request_digest,
            outcome,
            attempt.attempt_id,
            ordinal,
            AttemptState.FAILED,
            evidence_digest,
            blockers=tuple(run.blocked_details),
            remaining_attempts=remaining_after,
            next_safe_action=_next_action(outcome, remaining_after, ()),
            receipt_recorded=True,
            detail=recovered,
        )

    record = run.record
    verdict: FreshnessVerdict | None = None
    after_payload: dict[str, Any] | None
    try:
        after = ports.environment.observe()
        tree_after = dict(ports.test_tree())
        drift = before.drifted_from(after)
        if tree_after != tree_before:
            drift = (*drift, "test-tree-during-measurement")
        verdict = _freshness(record, after, request, candidate_digest)
        after_payload = after.to_payload()
    except (ValidationFailed, PolicyViolation, OSError):
        drift = ("post-measurement-observation-failed",)
        after_payload = None
    outcome = _classify(record, drift=drift, verdict=verdict)
    interrupted = record.status is MeasurementStatus.RUN_INCOMPLETE
    state = AttemptState.INTERRUPTED if interrupted else AttemptState.FAILED
    observations: tuple[Any, ...] = ()
    if outcome in {NativeOutcome.TARGET_MET, NativeOutcome.BELOW_TARGET}:
        observations = record.evidence.observations
        state = AttemptState.COMPLETED if observations else AttemptState.FAILED
        if not observations:
            outcome = NativeOutcome.INCOMPLETE
    evaluation = record.evaluation
    evidence_digest = _close_attempt(
        ports,
        attempt,
        state,
        {
            "outcome": outcome.value,
            "measurement": record.to_payload(),
            "measurement_record_digest": record.record_digest,
            "binding": None if record.binding is None else record.binding.to_payload(),
            "freshness": None if verdict is None else verdict.value,
            "drift": list(drift),
            "environment_before": before.to_payload(),
            "environment_after": after_payload,
            "test_tree_digest_before": fingerprint_tree(tree_before),
            "failed_cases": list(run.failed_cases),
            "output_bytes": run.output_bytes,
            "quality_verification": "ratio-observed-only",
            "authority": "observation-only",
        },
        observations,
    )
    blockers = tuple(record.blockers) if outcome is not NativeOutcome.TARGET_MET else ()
    return NativeMeasurement(
        request_digest,
        outcome,
        attempt.attempt_id,
        ordinal,
        state,
        evidence_digest,
        record.record_digest,
        record.status.value,
        record.evidence.exit_code,
        record.evidence.test_counts,
        tuple(o.to_payload() for o in record.evidence.observations),
        None if evaluation is None else (evaluation.aggregate_covered, evaluation.aggregate_total),
        blockers,
        tuple(run.failed_cases),
        None if verdict is None else verdict.value,
        drift,
        remaining_after,
        _next_action(outcome, remaining_after, tuple(run.failed_cases)),
        True,
        recovered,
    )


class _ShortTransactionLedger:
    """Her ledger islemini kendi kisa operational unit-of-work'unde commit eder.

    Surec kosusu sirasinda yazma kilidi tutulmaz ve claim, surec baslamadan ONCE kalicidir
    (claim-before-effect). Cokme halinde receipt'siz claim kalir ve bir sonraki olcumde
    ``INTERRUPTED`` olarak kapatilir.
    """

    def __init__(self, store: SQLiteOperationalStore) -> None:
        self._store = store

    def _run(self, action: Callable[[UnitTestLedger], Any]) -> Any:
        with self._store.unit_of_work() as uow:
            result = action(uow.unit_test_ledger())
            uow.commit()
            return result

    def register_request(self, request: UnitTestRequest, *, now: dt.datetime) -> Any:
        return self._run(lambda ledger: ledger.register_request(request, now=now))

    def claim_attempt(self, attempt: UnitTestAttempt, *, now: dt.datetime) -> Any:
        return self._run(lambda ledger: ledger.claim_attempt(attempt, now=now))

    def record_attempt_receipt(
        self,
        *,
        attempt_id: str,
        status: AttemptState,
        evidence_digest: str,
        observations: tuple[CoverageObservation, ...],
        now: dt.datetime,
    ) -> Any:
        return self._run(
            lambda ledger: ledger.record_attempt_receipt(
                attempt_id=attempt_id,
                status=status,
                evidence_digest=evidence_digest,
                observations=observations,
                now=now,
            )
        )

    def unreceipted_attempts(self, request_digest: str) -> tuple[str, ...]:
        return tuple(self._run(lambda ledger: ledger.unreceipted_attempts(request_digest)))

    def list_attempts(self, request_digest: str) -> tuple[UnitTestAttempt, ...]:
        return tuple(self._run(lambda ledger: ledger.list_attempts(request_digest)))

    def get_attempt_receipt(self, attempt_id: str) -> Any:
        return self._run(lambda ledger: ledger.get_attempt_receipt(attempt_id))

    def list_observations(self, attempt_id: str) -> tuple[CoverageObservation, ...]:
        return tuple(self._run(lambda ledger: ledger.list_observations(attempt_id)))

    def record_terminal(self, terminal: UnitTestTerminal, *, now: dt.datetime) -> Any:
        return self._run(lambda ledger: ledger.record_terminal(terminal, now=now))

    def list_terminals(self, request_digest: str) -> tuple[UnitTestTerminal, ...]:
        return tuple(self._run(lambda ledger: ledger.list_terminals(request_digest)))


def compose_native_measurement(
    request: UnitTestRequest,
    *,
    store: SQLiteOperationalStore,
    binding: MavenMeasurementBinding,
) -> NativeMeasurementPorts:
    """Gercek Maven olcer + ortam gozlemcisi + test agaci digest'i + mevcut operational ledger.

    OpenCode aranmaz; model/provider yoktur. Ledger ve artifact kayitlari kisa ayri
    transaction'larla yazilir; Maven calisirken veritabani kilidi tutulmaz.
    """

    parts = compose_maven_measurement(request, binding)
    module_dirs = discover_project(binding.project_root).module_dirs

    def register_artifact(artifact_digest: str, size_bytes: int, media_type: str) -> None:
        with store.unit_of_work() as uow:
            uow.register_artifact(
                artifact_digest=artifact_digest,
                media_type=media_type,
                size_bytes=size_bytes,
                classification="local-private",
            )
            uow.commit()

    return NativeMeasurementPorts(
        ledger=_ShortTransactionLedger(store),
        objects=parts.objects,
        measurer=parts.measurer,
        environment=parts.environment,
        test_tree=lambda: digest_test_tree(binding.project_root, module_dirs),
        plan_digest=binding.approved_maven_plan_digest,
        artifact_registrar=register_artifact,
    )
