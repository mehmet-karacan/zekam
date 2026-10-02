"""POM statik kesfi (plugin/extension CALISTIRMAZ): moduller, motor, JaCoCo, preflight bulgulari.

Burada ``help:effective-pom`` gibi plugin calistiran hicbir komut yoktur; sonuc yalniz
dosyalarin statik okunmasidir (profil/BOM/parent-resolution degerlendirilmez ve bu
``static_only`` olarak raporlanir). Tanilar acik bulgu olarak doner; sessizce duzeltilmez.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.unit_test_runner.path_safety import (
    read_file_digest,
    resolve_inside,
)
from zekam.infrastructure.unit_test_runner.safe_xml import XmlElement, parse_xml_bounded

MAX_MODULES: Final = 200
MAX_MODULE_DEPTH: Final = 8
_MAVEN_CONFIG_FILES: Final = (
    ".mvn/maven.config",
    ".mvn/jvm.config",
    ".mvn/extensions.xml",
    ".mvn/wrapper/maven-wrapper.properties",
)
_TRUE: Final = frozenset({"true"})


class JUnitEngine(StrEnum):
    JUNIT4 = "junit4"
    JUNIT_PLATFORM = "junit-platform"
    MIXED = "junit4+junit-platform"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class PomFinding:
    code: str
    severity: Severity
    module: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ModulePom:
    module: str
    packaging: str
    pom_digest: str
    engine: JUnitEngine
    compile_release: str | None
    has_jacoco_plugin: bool
    has_prepare_agent: bool
    jacoco_version: str | None
    surefire_version: str | None
    has_failsafe: bool
    findings: tuple[PomFinding, ...]


@dataclass(frozen=True, slots=True)
class ProjectDiscovery:
    modules: tuple[ModulePom, ...]
    config_digest: str
    static_only: bool = True

    @property
    def module_dirs(self) -> tuple[str, ...]:
        return tuple(m.module for m in self.modules)

    @property
    def findings(self) -> tuple[PomFinding, ...]:
        return tuple(f for m in self.modules for f in m.findings)

    def module(self, name: str) -> ModulePom | None:
        return next((m for m in self.modules if m.module == name), None)


def _walk(element: XmlElement) -> Iterator[XmlElement]:
    yield element
    for child in element.children:
        yield from _walk(child)


def _child(element: XmlElement, *path: str) -> XmlElement | None:
    current: XmlElement | None = element
    for tag in path:
        if current is None:
            return None
        found = current.find_all(tag)
        current = found[0] if found else None
    return current


def _text(element: XmlElement | None) -> str | None:
    return element.text if element is not None and element.text else None


def _plugins(project: XmlElement) -> list[XmlElement]:
    result: list[XmlElement] = []
    for container in (("build", "plugins"), ("build", "pluginManagement", "plugins")):
        holder = _child(project, *container)
        if holder is not None:
            result.extend(holder.find_all("plugin"))
    return result


def _dependencies(project: XmlElement) -> list[tuple[str, str]]:
    holder = _child(project, "dependencies")
    if holder is None:
        return []
    return [
        (_text(_child(dep, "groupId")) or "", _text(_child(dep, "artifactId")) or "")
        for dep in holder.find_all("dependency")
    ]


def _engine(dependencies: list[tuple[str, str]]) -> JUnitEngine:
    junit4 = any(g == "junit" and a == "junit" for g, a in dependencies)
    platform = any(
        (g.startswith("org.junit.") and g != "org.junit") or a.startswith("junit-jupiter")
        for g, a in dependencies
    ) or any(a == "junit-vintage-engine" for _, a in dependencies)
    if junit4 and platform:
        return JUnitEngine.MIXED
    if junit4:
        return JUnitEngine.JUNIT4
    if platform:
        return JUnitEngine.JUNIT_PLATFORM
    return JUnitEngine.UNKNOWN


def _property(project: XmlElement, name: str) -> str | None:
    return _text(_child(project, "properties", name))


def _inspect_pom(
    module: str,
    project: XmlElement,
    pom_digest: str,
    inherited: list[tuple[str, str]],
    inherited_plugins: dict[str, XmlElement],
) -> tuple[ModulePom, list[tuple[str, str]], dict[str, XmlElement]]:
    findings: list[PomFinding] = []

    def add(code: str, severity: Severity, detail: str = "") -> None:
        findings.append(PomFinding(code, severity, module, detail))

    own_dependencies = _dependencies(project)
    chain = inherited + own_dependencies
    plugins = {
        **inherited_plugins,
        **{_text(_child(p, "artifactId")) or "": p for p in _plugins(project)},
    }
    compiler = plugins.get("maven-compiler-plugin")
    release = (
        _property(project, "maven.compiler.release")
        or (_text(_child(compiler, "configuration", "release")) if compiler is not None else None)
        or _property(project, "maven.compiler.target")
        or _property(project, "maven.compiler.source")
    )

    jacoco = plugins.get("jacoco-maven-plugin")
    surefire = plugins.get("maven-surefire-plugin")
    prepare_agent = False
    if jacoco is not None:
        goals = {g.text for g in _walk(jacoco) if g.tag == "goal"}
        prepare_agent = "prepare-agent" in goals or "prepare-agent-integration" in goals
        if "prepare-agent-integration" in goals and "prepare-agent" not in goals:
            add("jacoco-integration-agent-only", Severity.BLOCKER, "unit icin prepare-agent yok")
        if any(e.tag == "exclude" for e in _walk(jacoco)):
            add("jacoco-exclusions", Severity.WARNING, "exclude kurali gorunur kalmali")
        if any(e.tag == "skip" and e.text.lower() in _TRUE for e in _walk(jacoco)):
            add("jacoco-skip", Severity.BLOCKER)
    if _property(project, "jacoco.skip") in _TRUE:
        add("jacoco-skip", Severity.BLOCKER, "property")
    for prop in ("skipTests", "maven.test.skip"):
        if (_property(project, prop) or "").lower() in _TRUE:
            add("tests-skipped", Severity.BLOCKER, prop)
    if surefire is not None:
        config = _child(surefire, "configuration")
        if config is not None:
            if _text(_child(config, "forkCount")) == "0":
                add("fork-count-zero", Severity.BLOCKER, "agent/JaCoCo olcumu guvenilmez")
            for flag in ("skip", "skipTests", "skipExec"):
                if (_text(_child(config, flag)) or "").lower() in _TRUE:
                    add("tests-skipped", Severity.BLOCKER, flag)
            if (_text(_child(config, "testFailureIgnore")) or "").lower() in _TRUE:
                add("test-failure-ignore", Severity.BLOCKER, "basarisiz testler gizlenir")
            arg_line = _text(_child(config, "argLine"))
            if jacoco is not None and arg_line and "argLine}" not in arg_line:
                add("argline-agent-override", Severity.BLOCKER, "JaCoCo agent'i ezilebilir")
            includes = [e.text for e in _walk(config) if e.tag == "include"]
            if any("IT" in item for item in includes):
                add("integration-in-unit-surefire", Severity.BLOCKER, "IT deseni dahil")
    failsafe = "maven-failsafe-plugin" in plugins
    if failsafe:
        add("failsafe-present", Severity.INFO, "lifecycle verify integration calistirir")
    for effect in ("exec-maven-plugin", "maven-antrun-plugin"):
        if effect in plugins:
            add("custom-exec-plugin", Severity.WARNING, effect)
    if _child(project, "profiles") is not None:
        add("profiles-not-evaluated", Severity.INFO, "statik kesif profilleri degerlendirmez")

    def version(plugin: XmlElement | None) -> str | None:
        return _text(_child(plugin, "version")) if plugin is not None else None

    engine = _engine(chain)
    if engine is JUnitEngine.UNKNOWN:
        add("engine-unknown", Severity.WARNING, "JUnit4/Platform dogrulanamadi: destek iddiasi yok")
    return (
        ModulePom(
            module=module,
            packaging=_text(_child(project, "packaging")) or "jar",
            pom_digest=pom_digest,
            engine=engine,
            compile_release=release,
            has_jacoco_plugin=jacoco is not None,
            has_prepare_agent=prepare_agent,
            jacoco_version=version(jacoco),
            surefire_version=version(surefire),
            has_failsafe=failsafe,
            findings=tuple(findings),
        ),
        chain,
        plugins,
    )


def discover_project(root: Path) -> ProjectDiscovery:
    """Kok ``pom.xml`` ve ``<modules>`` agacini statik okur; bulunamazsa ValidationFailed."""

    modules: list[ModulePom] = []
    stamps: list[list[str]] = []
    seen: set[str] = set()

    def visit(
        module: str,
        inherited: list[tuple[str, str]],
        inherited_plugins: dict[str, XmlElement],
        depth: int,
    ) -> None:
        if depth > MAX_MODULE_DEPTH or len(seen) >= MAX_MODULES or module in seen:
            raise ValidationFailed("Modul agaci siniri/dongu")
        seen.add(module)
        directory = resolve_inside(root, module)
        data, pom_digest = read_file_digest(directory / "pom.xml")
        project = parse_xml_bounded(data)
        if project.tag != "project":
            raise ValidationFailed("pom.xml 'project' koku tasimali")
        info, chain, merged = _inspect_pom(
            module, project, pom_digest, inherited, inherited_plugins
        )
        modules.append(info)
        stamps.append([module or ".", pom_digest])
        holder = _child(project, "modules")
        for item in holder.find_all("module") if holder is not None else []:
            child = f"{module}/{item.text}" if module else item.text
            visit(child, chain, merged, depth + 1)

    visit("", [], {}, 0)
    base = resolve_inside(root, "")
    for name in _MAVEN_CONFIG_FILES:
        candidate = base / name
        if candidate.exists():
            _, file_digest = read_file_digest(candidate)
            stamps.append([name, file_digest])
    if (base / ".mvn" / "extensions.xml").exists():
        extension = PomFinding("maven-extensions", Severity.WARNING, "", "extension kodu calisir")
        modules[0] = replace(modules[0], findings=(*modules[0].findings, extension))
    return ProjectDiscovery(tuple(modules), digest(sorted(stamps)))
