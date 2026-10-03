"""PIT (pitest-maven) plani: onayli kapsam, explicit argv, statik uyumluluk tanisi (W06 / D10).

PIT opsiyonel capability/adaptordur; Maven/pitest yoksa durum ``tool-missing`` /
``setup-required`` olur ve temel akis etkilenmez (rapor ``mutation_not_run`` der).

Kurallar:

- Hedef sinif, hedef test, mutator ve timeout butcesi kullanicinin onayladigi degerlerdir;
  hicbiri varsayilana birakilmaz. Hepsi plana ``-D`` argumani olarak yazilir; POM
  ``<configuration>`` bunlari ezecegi icin POM'da ayni anahtar varsa ``setup-required``
  doner (sessiz override yok).
- Goal, POM'da bildirilmis plugin surumuyle tam nitelenir
  (``org.pitest:pitest-maven:<surum>:mutationCoverage``). Surum POM'da yoksa ya da
  cozulemezse ``setup-required``: otomatik surum cozumleme/upgrade ve POM yazimi YOKTUR;
  eksikler yalniz ``setup_plan`` metni olarak onerilir.
- Plugin/engine uyumlulugu POM'un statik incelemesiyle tanilanir: JUnit Platform
  (JUnit5) modulu icin ``pitest-junit5-plugin`` bagimliligi pitest plugin'inde bildirilmis
  olmali. Surum cifti dogrulanmaz (``pit-plugin-pair-unverified`` INFO); gercek PIT
  kosusu bu kodla dogrulanmis degildir.
- Yetki: plan hem ``plugin-execution`` hem de ayri ``bytecode-mutation`` etkisi tasir;
  kaynak Java dosyalarina sahte hata yazan yol yoktur (PIT yalniz ``target/classes``
  bytecode'unu kendi isleminde mutasyona ugratir). ``network_possible`` her zaman True'dur.
- Sinif/test desenlerinde ``$`` yoktur (guvenli arguman kumesi); ic siniflar icin
  ``p.A*`` kullanilir. Birden cok modullu projede ``-pl <modul> -am`` kullanilir ve
  upstream modullerde mutant bulunmamasi hata sayilmasin diye ``failWhenNoMutations=false``
  verilir; hedef modulde 0 mutant ise rapor 0 sayacla gorunur, sahte skor uretilmez.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.unit_test_runner.maven_plan import (
    ExecutionAuthorization,
    ExecutionClass,
    MavenPlan,
    PlanStatus,
    build_version_probe_plan,
    check_authorization,
)
from zekam.infrastructure.unit_test_runner.path_safety import (
    is_link_or_junction,
    read_file_digest,
    resolve_inside,
)
from zekam.infrastructure.unit_test_runner.pom_inspect import (
    JUnitEngine,
    PomFinding,
    ProjectDiscovery,
    Severity,
    discover_project,
)
from zekam.infrastructure.unit_test_runner.safe_xml import XmlElement, parse_xml_bounded

PIT_PLAN_CONTRACT: Final = "zekam-pit-plan/v1"
PIT_GROUP: Final = "org.pitest"
PIT_ARTIFACT: Final = "pitest-maven"
JUNIT5_PLUGIN_ARTIFACT: Final = "pitest-junit5-plugin"
EFFECT_PLUGIN: Final = "plugin-execution"
EFFECT_BYTECODE: Final = "bytecode-mutation"
MAX_LIST: Final = 50

_CLASS_PATTERN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,150}\*?$")
_MUTATOR_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,60}$")
_FACTOR_PATTERN: Final = re.compile(r"^[1-9](\.[0-9]{1,2})?$")
_VERSION_PATTERN: Final = re.compile(r"^[0-9][A-Za-z0-9_.+-]{0,40}$")
_PROPERTY_REF: Final = re.compile(r"^\$\{([A-Za-z0-9_.-]+)\}$")
#: Plan argumani olarak verilen anahtarlar; POM <configuration> icinde bulunursa ezilir.
_PLAN_CONTROLLED: Final = frozenset(
    {
        "targetClasses", "targetTests", "mutators", "outputFormats", "timestampedReports",
        "reportsDirectory", "skip", "timeoutConstant", "timeoutFactor", "threads",
        "maxMutationsPerClass", "failWhenNoMutations", "excludedClasses", "excludedMethods",
        "excludedTestClasses", "avoidCallsTo", "features",
    }
)  # fmt: skip
_HISTORY_KEYS: Final = frozenset({"historyInputFile", "historyOutputFile", "withHistory"})
_THRESHOLD_KEYS: Final = frozenset({"mutationThreshold", "coverageThreshold"})


@dataclass(frozen=True, slots=True)
class PitApproval:
    """Kullanicinin onayladigi exact PIT kapsami ve butcesi (varsayilan yok)."""

    module: str
    target_classes: tuple[str, ...]
    target_tests: tuple[str, ...]
    mutators: tuple[str, ...]
    timeout_constant_ms: int
    timeout_factor: str
    process_timeout_seconds: int
    max_mutations_per_class: int | None = None

    def __post_init__(self) -> None:
        for label, items, pattern in (
            ("hedef sinif", self.target_classes, _CLASS_PATTERN),
            ("hedef test", self.target_tests, _CLASS_PATTERN),
            ("mutator", self.mutators, _MUTATOR_PATTERN),
        ):
            if not items or len(items) > MAX_LIST:
                raise ValidationFailed(f"{label} listesi bos olamaz ve {MAX_LIST}'i asamaz")
            if len(set(items)) != len(items) or any(pattern.fullmatch(i) is None for i in items):
                raise ValidationFailed(f"{label} guvenli desenle eslesmiyor ya da tekrar ediyor")
        if not 100 <= self.timeout_constant_ms <= 600_000:
            raise ValidationFailed("timeoutConstant 100..600000 ms araliginda olmali")
        if _FACTOR_PATTERN.fullmatch(self.timeout_factor) is None:
            raise ValidationFailed("timeoutFactor 1..9.99 araliginda olmali")
        try:
            Decimal(self.timeout_factor)
        except InvalidOperation as exc:
            raise ValidationFailed("timeoutFactor sayi degil") from exc
        if not 1 <= self.process_timeout_seconds <= 24 * 3600:
            raise ValidationFailed("PIT surec butcesi 1..86400 saniye olmali")
        if self.max_mutations_per_class is not None and not (
            1 <= self.max_mutations_per_class <= 10_000
        ):
            raise ValidationFailed("maxMutationsPerClass 1..10000 olmali")
        if self.module and (
            not re.fullmatch(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*", self.module)
            or any(part in {".", ".."} for part in self.module.split("/"))
        ):
            raise ValidationFailed("modul yolu guvenli desenle eslesmiyor")

    def to_payload(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "target_classes": list(self.target_classes),
            "target_tests": list(self.target_tests),
            "mutators": list(self.mutators),
            "timeout_constant_ms": self.timeout_constant_ms,
            "timeout_factor": self.timeout_factor,
            "process_timeout_seconds": self.process_timeout_seconds,
            "max_mutations_per_class": self.max_mutations_per_class,
        }


@dataclass(frozen=True, slots=True)
class PitPlan:
    maven_plan: MavenPlan
    approval: PitApproval
    plugin_version: str
    junit5_plugin_version: str | None
    report_path: str
    findings: tuple[PomFinding, ...]
    test_candidate_digest: str
    effects: tuple[str, ...] = (EFFECT_PLUGIN, EFFECT_BYTECODE)

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract": PIT_PLAN_CONTRACT,
            "maven_plan_digest": self.maven_plan.plan_digest,
            "approval": self.approval.to_payload(),
            "plugin_version": self.plugin_version,
            "junit5_plugin_version": self.junit5_plugin_version,
            "report_path": self.report_path,
            "test_candidate_digest": self.test_candidate_digest,
            "effects": list(self.effects),
            "findings": [[f.code, f.severity.value, f.module, f.detail] for f in self.findings],
        }

    @property
    def pit_plan_digest(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class PitPlanResult:
    status: PlanStatus
    plan: PitPlan | None
    reasons: tuple[str, ...]
    setup_plan: tuple[str, ...]
    findings: tuple[PomFinding, ...]


@dataclass(frozen=True, slots=True)
class PitAuthorization:
    """Exact PIT plan digest'ine ve ayri bytecode-mutation etkisine bagli onay."""

    pit_plan_digest: str
    effects: tuple[str, ...]
    allow_network: bool = False


def check_pit_authorization(plan: PitPlan, authorization: PitAuthorization | None) -> None:
    if authorization is None:
        raise PolicyViolation("PIT yurutme yetkisi yok")
    if authorization.pit_plan_digest != plan.pit_plan_digest:
        raise PolicyViolation("PIT yetkisi baska bir plana ait (digest uyusmuyor)")
    if not set(plan.effects) <= set(authorization.effects):
        raise PolicyViolation("PIT yetkisi bytecode-mutation etkisini acikca kapsamiyor")
    if plan.maven_plan.network_possible and not authorization.allow_network:
        raise PolicyViolation("Plan ag erisimi olasi; ayri ag yetkisi gerekir")


def maven_authorization_for(
    plan: PitPlan, authorization: PitAuthorization | None
) -> ExecutionAuthorization:
    """PIT yetkisi dogrulandiktan sonra icteki Maven yetkisini turetir."""

    check_pit_authorization(plan, authorization)
    assert authorization is not None
    inner = ExecutionAuthorization(
        plan.maven_plan.plan_digest,
        plan.maven_plan.execution_class,
        allow_network=authorization.allow_network,
    )
    check_authorization(plan.maven_plan, inner)
    return inner


# ------------------------------------------------------------------ POM statik inceleme


def _walk(element: XmlElement) -> Iterator[XmlElement]:
    yield element
    for child in element.children:
        yield from _walk(child)


def _first(element: XmlElement | None, *path: str) -> XmlElement | None:
    current = element
    for tag in path:
        if current is None:
            return None
        found = current.find_all(tag)
        current = found[0] if found else None
    return current


def _text(element: XmlElement | None) -> str | None:
    return element.text if element is not None and element.text else None


def _chain(module: str) -> tuple[str, ...]:
    parts = module.split("/") if module else []
    return ("", *("/".join(parts[: i + 1]) for i in range(len(parts))))


@dataclass(frozen=True, slots=True)
class PitPomInfo:
    plugin: XmlElement | None
    version: str | None
    version_unresolved: bool
    junit5_plugin_version: str | None
    has_junit5_plugin: bool
    config_keys: frozenset[str]


def inspect_pit_pom(root: Path, module: str) -> PitPomInfo:
    """Kok->modul POM zincirini statik okur; plugin cocugu ebeveyni ezer (plugin CALISTIRMAZ)."""

    properties: dict[str, str] = {}
    plugin: XmlElement | None = None
    for directory in _chain(module):
        data, _ = read_file_digest(resolve_inside(root, directory) / "pom.xml")
        project = parse_xml_bounded(data)
        props = _first(project, "properties")
        for prop in props.children if props is not None else []:
            if prop.text:
                properties[prop.tag] = prop.text
        for location in (("build", "plugins"), ("build", "pluginManagement", "plugins")):
            plugins = _first(project, *location)
            for candidate in plugins.find_all("plugin") if plugins is not None else []:
                if _text(_first(candidate, "artifactId")) == PIT_ARTIFACT:
                    plugin = candidate
    if plugin is None:
        return PitPomInfo(None, None, False, None, False, frozenset())
    raw_version = _text(_first(plugin, "version"))
    version = raw_version
    ref = _PROPERTY_REF.fullmatch(raw_version) if raw_version else None
    if ref is not None:
        version = properties.get(ref.group(1))
    valid = version is not None and _VERSION_PATTERN.fullmatch(version) is not None
    holder = _first(plugin, "dependencies")
    junit5 = next(
        (
            dep
            for dep in (holder.find_all("dependency") if holder is not None else [])
            if _text(_first(dep, "artifactId")) == JUNIT5_PLUGIN_ARTIFACT
        ),
        None,
    )
    keys = frozenset(e.tag for e in _walk(plugin) if e.tag not in {"plugin"})
    return PitPomInfo(
        plugin,
        version if valid else None,
        not valid,
        _text(_first(junit5, "version")) if junit5 is not None else None,
        junit5 is not None,
        keys,
    )


MAX_TEST_FILES: Final = 20_000


def compute_test_candidate_digest(root: Path, module: str) -> str:
    """Hedef modulun ``src/test`` agacinin (yol + icerik digest'i) deterministik digest'i.

    Maven plani test ADAYINA baglanmadigi icin PIT sonucu bu digest ile ayrica baglanir.
    Baglanti/junction iceren agac reddedilir; dosya sayisi ve boyutu sinirlidir.
    """

    relative = f"{module}/src/test" if module else "src/test"
    base = resolve_inside(root, relative)
    entries: list[list[str]] = []
    if base.is_dir():
        for current, dirs, files in os.walk(base, followlinks=False):
            current_path = Path(current)
            for name in dirs:
                if is_link_or_junction(current_path / name):
                    raise PolicyViolation("Test agacinda symlink/junction reddedildi")
            dirs.sort()
            for name in sorted(files):
                if len(entries) >= MAX_TEST_FILES:
                    raise ValidationFailed("Test agaci dosya siniri asildi")
                _, file_digest = read_file_digest(current_path / name)
                entries.append([(current_path / name).relative_to(base).as_posix(), file_digest])
    return digest(sorted(entries))


def _setup_snippet(engine: JUnitEngine) -> tuple[str, ...]:
    steps = [
        "pom.xml build/plugins altina org.pitest:pitest-maven icin SABIT surumlu plugin "
        "tanimi ekleyin (surum aralik/LATEST olamaz); bu plan POM'a yazmaz",
    ]
    if engine in {JUnitEngine.JUNIT_PLATFORM, JUnitEngine.MIXED}:
        steps.append(
            "JUnit Platform icin pitest plugin'ine org.pitest:pitest-junit5-plugin bagimliligi "
            "ekleyin; pitest ve junit5-plugin surum uyumlulugunu proje belgelerinden dogrulayin"
        )
    steps.append("Bagimlilik/plugin indirme ve POM degisikligi ayri exact onay ister")
    return tuple(steps)


def build_pit_plan(
    project_root: Path,
    approval: PitApproval,
    *,
    allow_wrapper_download: bool = False,
    search_path: str | None = None,
) -> PitPlanResult:
    """PIT plani. Eksiklikte olcum uydurmaz; ``tool-missing``/``setup-required`` doner."""

    root = project_root.resolve(strict=True)
    if not (root / "pom.xml").is_file():
        return PitPlanResult(
            PlanStatus.NOT_SUPPORTED, None, ("pom.xml yok: Maven disi (not-supported)",), (), ()
        )
    discovery = discover_project(root)
    info = discovery.module(approval.module)
    if info is None:
        return PitPlanResult(
            PlanStatus.NOT_SUPPORTED,
            None,
            (f"hedef modul bulunamadi: {approval.module or '.'}",),
            (),
            (),
        )
    pom = inspect_pit_pom(root, approval.module)
    findings, reasons, setup = _diagnose(approval, discovery, pom, info.engine)
    if reasons or setup:
        if setup:
            setup += _setup_snippet(info.engine)
        return PitPlanResult(
            PlanStatus.SETUP_REQUIRED, None, tuple(reasons), tuple(setup), tuple(findings)
        )
    assert pom.version is not None
    probe = build_version_probe_plan(
        root, search_path=search_path, allow_wrapper_download=allow_wrapper_download
    )
    if probe.plan is None:
        steps = (
            "Proje wrapper'i (mvnw) ya da PATH'te mvn bulunamadi; kurulum/onarim ayri onay ister",
            "Bu makinede Maven/PIT kurulmaz ve mutation sonucu uydurulmaz",
        )
        return PitPlanResult(PlanStatus.TOOL_MISSING, None, probe.reasons, steps, tuple(findings))
    goal = f"{PIT_GROUP}:{PIT_ARTIFACT}:{pom.version}:mutationCoverage"
    args: list[str] = ["-B"]
    if approval.module:
        args += ["-pl", approval.module, "-am", "-DfailWhenNoMutations=false"]
    args += [
        f"-DtargetClasses={','.join(approval.target_classes)}",
        f"-DtargetTests={','.join(approval.target_tests)}",
        f"-Dmutators={','.join(approval.mutators)}",
        f"-DtimeoutConstant={approval.timeout_constant_ms}",
        f"-DtimeoutFactor={approval.timeout_factor}",
        "-Dthreads=1",
        "-DoutputFormats=XML",
        "-DtimestampedReports=false",
    ]
    if approval.max_mutations_per_class is not None:
        args.append(f"-DmaxMutationsPerClass={approval.max_mutations_per_class}")
    goals = ("test-compile", goal)
    args += list(goals)
    declared = [(PIT_ARTIFACT, pom.version)]
    if pom.junit5_plugin_version:
        declared.append((JUNIT5_PLUGIN_ARTIFACT, pom.junit5_plugin_version))
    maven_plan = MavenPlan(
        project_root=str(root),
        launcher=probe.plan.launcher,
        args=tuple(args),
        execution_class=ExecutionClass.PLUGIN_EXECUTION,
        goals=goals,
        modules=discovery.module_dirs,
        target_modules=(approval.module,),
        declared_plugins=tuple(sorted(declared)),
        expected_plugin_executions=(f"{PIT_ARTIFACT}:mutationCoverage",),
        config_digest=discovery.config_digest,
        network_possible=True,
        scope="mutation",
    )
    report = (
        f"{approval.module}/target/pit-reports/mutations.xml"
        if approval.module
        else "target/pit-reports/mutations.xml"
    )
    plan = PitPlan(
        maven_plan,
        approval,
        pom.version,
        pom.junit5_plugin_version,
        report,
        tuple(findings),
        compute_test_candidate_digest(root, approval.module),
    )
    return PitPlanResult(PlanStatus.READY, plan, probe.reasons, (), tuple(findings))


def _diagnose(
    approval: PitApproval,
    discovery: ProjectDiscovery,
    pom: PitPomInfo,
    engine: JUnitEngine,
) -> tuple[list[PomFinding], list[str], list[str]]:
    module = approval.module
    findings: list[PomFinding] = []
    reasons: list[str] = []
    setup: list[str] = []

    def add(code: str, severity: Severity, detail: str = "") -> None:
        findings.append(PomFinding(code, severity, module, detail))
        if severity is Severity.BLOCKER:
            reasons.append(f"{module or '.'}: {code}")

    if pom.plugin is None:
        add("pit-plugin-missing", Severity.BLOCKER, "pitest-maven POM'da bildirilmemis")
        setup.append(f"{module or '.'}: pitest-maven plugin'i bildirilmemis")
        return findings, reasons, setup
    if pom.version is None:
        add("pit-plugin-version-unresolved", Severity.BLOCKER, "sabit surum statik cozulemedi")
        setup.append(f"{module or '.'}: pitest-maven surumu sabit/cozulebilir olmali")
    if engine is JUnitEngine.UNKNOWN:
        add("engine-unknown", Severity.BLOCKER, "JUnit4/Platform dogrulanamadi: destek iddiasi yok")
    if engine in {JUnitEngine.JUNIT_PLATFORM, JUnitEngine.MIXED}:
        if not pom.has_junit5_plugin:
            add("pit-junit5-plugin-missing", Severity.BLOCKER, "JUnit Platform icin gerekli")
            setup.append(f"{module or '.'}: pitest-junit5-plugin bagimliligi eksik")
        else:
            add(
                "pit-plugin-pair-unverified",
                Severity.INFO,
                "pitest/junit5-plugin cifti dogrulanmadi",
            )
    for key in sorted(pom.config_keys & _PLAN_CONTROLLED):
        add("pit-pom-config-overrides-plan", Severity.BLOCKER, key)
        setup.append(f"{module or '.'}: POM '{key}' onayli plan degerini ezer")
    for key in sorted(pom.config_keys & _HISTORY_KEYS):
        add(
            "pit-history-configured", Severity.WARNING, f"{key}: eski sonuc yeni kosuya karisabilir"
        )
    for key in sorted(pom.config_keys & _THRESHOLD_KEYS):
        add(
            "pit-threshold-in-pom",
            Severity.WARNING,
            f"{key}: build exit code esik nedeniyle degisebilir",
        )
    own = discovery.module(module)
    if own is not None:
        for finding in own.findings:
            if finding.code == "tests-skipped":
                add("tests-skipped", Severity.BLOCKER, finding.detail)
    return findings, reasons, setup
