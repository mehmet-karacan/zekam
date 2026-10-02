"""Maven plan uretimi: dogrulanmis launcher, explicit argv, plugin kaydi, yetki sinifi (W04).

Kurallar:

- Launcher ya proje kokundeki wrapper'dir (digest + ``maven-wrapper.properties`` digest'i
  plana yazilir; drift calistirmada reddedilir) ya da mutlak yoldaki kurulu ``mvn``.
- Plan serbest metin tasimaz: argv, plan builder'in sabit anahtarlarindan ve dogrulanmis
  degerlerden uretilir. ``shell=True`` yoktur.
- ``mvn test`` tek basina olcum garantisi degildir: hedef goal dizisi unit ``test`` ve
  ``jacoco:report``'tur; JaCoCo/POM altyapisi eksikse ``SETUP_REQUIRED`` doner ve
  sessiz POM degisikligi yapilmaz.
- Plugin calistiran her komut (``help:effective-pom`` dahil) ``PLUGIN_EXECUTION``
  sinifindadir ve calistirmada exact authorization ister.
- Ag izolasyonu verilmez: ``network_isolation`` daima ``not-provided`` yazilir.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.unit_test_runner.path_safety import (
    is_link_or_junction,
    read_file_digest,
)
from zekam.infrastructure.unit_test_runner.pom_inspect import (
    JUnitEngine,
    ProjectDiscovery,
    Severity,
    discover_project,
)

PLAN_CONTRACT: Final = "zekam-maven-plan/v1"
#: .cmd/.bat koprusu icin tum argv elemanlarinda izinli karakterler (metakarakter yok).
SAFE_ARG: Final = re.compile(r"^[A-Za-z0-9_.,:=@#+*/\\-]{1,256}$")
_SAFE_TEST_SELECTION: Final = re.compile(r"^[A-Za-z0-9_.*#,]{1,200}$")
_DISTRIBUTION_URL: Final = re.compile(r"(?m)^\s*distributionUrl\s*=\s*(\S+)")
_VERSION_RE: Final = re.compile(r"Apache Maven\s+(\S+)")
_JAVA_RE: Final = re.compile(r"Java version:\s*([^,\s]+)")


class ExecutionClass(StrEnum):
    TOOL_PROBE = "tool-probe"  # mvn -v: plugin calistirmaz
    PLUGIN_EXECUTION = "plugin-execution"  # test/report/help:*; plugin ve test kodu calisir


class PlanStatus(StrEnum):
    READY = "ready"
    TOOL_MISSING = "tool-missing"
    SETUP_REQUIRED = "setup-required"
    NOT_SUPPORTED = "not-supported"


class LauncherKind(StrEnum):
    WRAPPER = "wrapper"
    INSTALLED = "installed"


@dataclass(frozen=True, slots=True)
class LauncherIdentity:
    kind: LauncherKind
    path: str
    digest: str
    wrapper_properties_digest: str | None
    distribution_url: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "path": self.path,
            "digest": self.digest,
            "wrapper_properties_digest": self.wrapper_properties_digest,
            "distribution_url": self.distribution_url,
        }

    @property
    def bridge_required(self) -> bool:
        return self.path.lower().endswith((".cmd", ".bat"))


@dataclass(frozen=True, slots=True)
class MavenPlan:
    project_root: str
    launcher: LauncherIdentity
    args: tuple[str, ...]
    execution_class: ExecutionClass
    goals: tuple[str, ...]
    modules: tuple[str, ...]
    target_modules: tuple[str, ...]
    declared_plugins: tuple[tuple[str, str], ...]
    expected_plugin_executions: tuple[str, ...]
    config_digest: str
    network_possible: bool
    scope: str = "unit"
    network_isolation: str = "not-provided"

    def __post_init__(self) -> None:
        for arg in self.args:
            if SAFE_ARG.fullmatch(arg) is None:
                raise ValidationFailed("Plan argumani guvenli karakter kumesi disinda")

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.launcher.path, *self.args)

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract": PLAN_CONTRACT,
            "project_root": self.project_root,
            "launcher": self.launcher.to_payload(),
            "args": list(self.args),
            "execution_class": self.execution_class.value,
            "goals": list(self.goals),
            "modules": list(self.modules),
            "target_modules": list(self.target_modules),
            "declared_plugins": [list(item) for item in self.declared_plugins],
            "expected_plugin_executions": list(self.expected_plugin_executions),
            "config_digest": self.config_digest,
            "network_possible": self.network_possible,
            "scope": self.scope,
            "network_isolation": self.network_isolation,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class PlanResult:
    status: PlanStatus
    plan: MavenPlan | None
    reasons: tuple[str, ...]
    setup_plan: tuple[str, ...]
    discovery: ProjectDiscovery | None


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    """Exact plan digest'ine ve yurutme sinifina bagli onay; ag ayrica acilir."""

    plan_digest: str
    execution_class: ExecutionClass
    allow_network: bool = False


def check_authorization(plan: MavenPlan, authorization: ExecutionAuthorization | None) -> None:
    if authorization is None:
        raise PolicyViolation("Maven yurutme yetkisi yok")
    if authorization.plan_digest != plan.plan_digest:
        raise PolicyViolation("Yetki baska bir plana ait (plan digest uyusmuyor)")
    if authorization.execution_class is not plan.execution_class:
        raise PolicyViolation("Yetki yurutme sinifi planla uyusmuyor")
    if plan.network_possible and not authorization.allow_network:
        raise PolicyViolation("Plan ag erisimi olasi; ayri ag yetkisi gerekir")


def _wrapper_identity(root: Path) -> LauncherIdentity | None:
    name = "mvnw.cmd" if os.name == "nt" else "mvnw"
    script = root / name
    if not script.exists():
        return None
    if is_link_or_junction(script):
        raise PolicyViolation("Wrapper symlink/junction olamaz")
    _, script_digest = read_file_digest(script)
    properties = root / ".mvn" / "wrapper" / "maven-wrapper.properties"
    props_digest: str | None = None
    url: str | None = None
    if properties.is_file() and not is_link_or_junction(properties):
        data, props_digest = read_file_digest(properties)
        match = _DISTRIBUTION_URL.search(data.decode("utf-8", errors="replace"))
        url = match.group(1) if match else None
    return LauncherIdentity(
        LauncherKind.WRAPPER, str(script.resolve(strict=True)), script_digest, props_digest, url
    )


def _installed_identity(search_path: str | None) -> LauncherIdentity | None:
    names = ("mvn.cmd", "mvn.bat") if os.name == "nt" else ("mvn",)
    for name in names:
        found = shutil.which(name, path=search_path)
        if found is None:
            continue
        path = Path(found)
        if not path.is_absolute() or is_link_or_junction(path):
            continue
        _, file_digest = read_file_digest(path)
        return LauncherIdentity(
            LauncherKind.INSTALLED, str(path.resolve(strict=True)), file_digest, None, None
        )
    return None


def verify_launcher(launcher: LauncherIdentity) -> None:
    """Wrapper/launcher drift'ini calistirma aninda tekrar dogrular."""

    path = Path(launcher.path)
    if is_link_or_junction(path) or not path.is_file():
        raise PolicyViolation("Launcher kayip ya da baglanti")
    _, current = read_file_digest(path)
    if current != launcher.digest:
        raise PolicyViolation("Launcher digest'i plandan farkli (wrapper drift)")
    if launcher.kind is LauncherKind.WRAPPER:
        properties = path.parent / ".mvn" / "wrapper" / "maven-wrapper.properties"
        if launcher.wrapper_properties_digest is None:
            if properties.exists():
                raise PolicyViolation("Wrapper properties plandan sonra belirdi (drift)")
        else:
            _, props_now = read_file_digest(properties)
            if props_now != launcher.wrapper_properties_digest:
                raise PolicyViolation("maven-wrapper.properties plandan farkli (drift)")


def parse_version_output(output: str) -> tuple[str | None, str | None]:
    """``mvn -v`` ciktisindan (maven, java) surumu; arac kimligi, olcum sonucu degildir."""

    maven = _VERSION_RE.search(output)
    java = _JAVA_RE.search(output)
    return (maven.group(1) if maven else None, java.group(1) if java else None)


def _resolve_launcher(
    root: Path, *, allow_wrapper_download: bool, search_path: str | None
) -> tuple[LauncherIdentity | None, list[str]]:
    notes: list[str] = []
    wrapper = _wrapper_identity(root)
    if wrapper is not None and (allow_wrapper_download or wrapper.distribution_url is None):
        return wrapper, notes
    if wrapper is not None:
        notes.append(
            "wrapper dagitim indirmesi ag gerektirebilir; ayri izin yok, kurulu mvn aranir"
        )
    return _installed_identity(search_path), notes


def build_unit_test_plan(
    project_root: Path,
    *,
    target_modules: tuple[str, ...],
    test_selection: tuple[str, ...] = (),
    allow_wrapper_download: bool = False,
    search_path: str | None = None,
) -> PlanResult:
    """Unit-test + JaCoCo olcum plani. Eksiklikte olcum uydurmaz; setup plani doner."""

    root = project_root.resolve(strict=True)
    if not (root / "pom.xml").is_file():
        return PlanResult(
            PlanStatus.NOT_SUPPORTED,
            None,
            ("pom.xml yok: Maven disi/tanimsiz teknoloji (not-supported)",),
            (),
            None,
        )
    discovery = discover_project(root)
    reasons: list[str] = []
    setup: list[str] = []
    for module in target_modules:
        info = discovery.module(module)
        if info is None:
            return PlanResult(
                PlanStatus.NOT_SUPPORTED,
                None,
                (f"hedef modul bulunamadi: {module or '.'}",),
                (),
                discovery,
            )
        if info.engine is JUnitEngine.UNKNOWN:
            reasons.append(f"{module or '.'}: JUnit4/Platform dogrulanamadi")
        if not (info.has_jacoco_plugin and info.has_prepare_agent):
            setup.append(
                f"{module or '.'}: jacoco-maven-plugin prepare-agent execution'i tanimli degil"
            )
    blockers = [f for f in discovery.findings if f.severity is Severity.BLOCKER]
    reasons.extend(f"{f.module or '.'}: {f.code}" for f in blockers)
    if reasons or setup:
        if setup:
            setup.append(
                "POM/parent/argLine degisikligi ayri exact onay ister; bu plan degistirmez"
            )
        return PlanResult(PlanStatus.SETUP_REQUIRED, None, tuple(reasons), tuple(setup), discovery)

    launcher, notes = _resolve_launcher(
        root, allow_wrapper_download=allow_wrapper_download, search_path=search_path
    )
    if launcher is None:
        steps = (
            "Proje wrapper'i (mvnw) ya da PATH'te mvn bulunamadi; kurulum/onarim ayri onay ister",
            "Bu makinede Maven kurulmaz ve olcum uydurulmaz",
        )
        return PlanResult(PlanStatus.TOOL_MISSING, None, tuple(notes), steps, discovery)

    args: list[str] = ["-B"]
    non_root = tuple(m for m in target_modules if m)
    if non_root:
        args += ["-pl", ",".join(non_root), "-am"]
    if test_selection:
        for item in test_selection:
            if _SAFE_TEST_SELECTION.fullmatch(item) is None:
                raise ValidationFailed("Test secimi guvenli desenle eslesmiyor")
        args += [f"-Dtest={','.join(test_selection)}", "-Dsurefire.failIfNoSpecifiedTests=false"]
    goals = ("test", "jacoco:report")
    args += list(goals)
    plugins = tuple(
        sorted(
            {
                ("jacoco-maven-plugin", m.jacoco_version or "unresolved")
                for m in discovery.modules
                if m.has_jacoco_plugin
            }
            | {
                ("maven-surefire-plugin", m.surefire_version or "unresolved")
                for m in discovery.modules
            }
        )
    )
    plan = MavenPlan(
        project_root=str(root),
        launcher=launcher,
        args=tuple(args),
        execution_class=ExecutionClass.PLUGIN_EXECUTION,
        goals=goals,
        modules=discovery.module_dirs,
        target_modules=target_modules,
        declared_plugins=plugins,
        expected_plugin_executions=("maven-surefire-plugin:test", "jacoco-maven-plugin:report"),
        config_digest=discovery.config_digest,
        network_possible=True,
    )
    return PlanResult(PlanStatus.READY, plan, tuple(notes), (), discovery)


def _single_command_plan(
    project_root: Path,
    args: tuple[str, ...],
    execution_class: ExecutionClass,
    *,
    search_path: str | None,
    allow_wrapper_download: bool,
) -> PlanResult:
    root = project_root.resolve(strict=True)
    launcher, notes = _resolve_launcher(
        root, allow_wrapper_download=allow_wrapper_download, search_path=search_path
    )
    if launcher is None:
        return PlanResult(
            PlanStatus.TOOL_MISSING, None, tuple(notes), ("mvnw veya PATH'te mvn yok",), None
        )
    plan = MavenPlan(
        project_root=str(root),
        launcher=launcher,
        args=args,
        execution_class=execution_class,
        goals=tuple(a for a in args if not a.startswith("-")),
        modules=(),
        target_modules=(),
        declared_plugins=(),
        expected_plugin_executions=(),
        config_digest=digest([]),
        network_possible=execution_class is ExecutionClass.PLUGIN_EXECUTION,
        scope="probe",
    )
    return PlanResult(PlanStatus.READY, plan, tuple(notes), (), None)


def build_version_probe_plan(
    project_root: Path, *, search_path: str | None = None, allow_wrapper_download: bool = False
) -> PlanResult:
    """``mvn -v`` (plugin calistirmaz; wrapper ilk kullanimda ag gerektirebilir)."""

    return _single_command_plan(
        project_root,
        ("-v",),
        ExecutionClass.TOOL_PROBE,
        search_path=search_path,
        allow_wrapper_download=allow_wrapper_download,
    )


def build_effective_pom_plan(
    project_root: Path, *, search_path: str | None = None, allow_wrapper_download: bool = False
) -> PlanResult:
    """``help:effective-pom`` plugin calistirir: PLUGIN_EXECUTION sinifi, exact yetki ister."""

    return _single_command_plan(
        project_root,
        ("-B", "help:effective-pom"),
        ExecutionClass.PLUGIN_EXECUTION,
        search_path=search_path,
        allow_wrapper_download=allow_wrapper_download,
    )
