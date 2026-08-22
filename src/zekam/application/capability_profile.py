"""Proje capability profili.

Profil deterministiktir: ayni kaynak surumu ve ayni uretici surumu her zaman ayni
`profile_digest` degerini uretir. Her tespit, hangi dosyanin hangi kanitla bu sonuca
yol actigini soyler; kanitsiz tahmin uretilmez.

Profil olusturulurken kaynak agacina yazilmaz, komut calistirilmaz, bagimlilik
kurulmaz ve ag erisimi yapilmaz. Yalnizca kesif sirasinda gecen manifest dosyalari
okunur.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.source_discovery import DiscoveryReport
from zekam.domain.canonical import digest

#: Uretici surumu. Kurallar degisirse artirilir ve eski profiller stale olur.
PROFILER_VERSION = "zekam-capability-profiler/v1"

#: Uzanti -> dil eslesmesi.
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    "py": "python",
    "pyi": "python",
    "java": "java",
    "kt": "kotlin",
    "kts": "kotlin",
    "js": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "go": "go",
    "rs": "rust",
    "rb": "ruby",
    "php": "php",
    "cs": "csharp",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "hpp": "cpp",
    "sql": "sql",
    "pks": "plsql",
    "pkb": "plsql",
    "sh": "shell",
    "ps1": "powershell",
    "yaml": "yaml",
    "yml": "yaml",
    "json": "json",
    "toml": "toml",
    "md": "markdown",
    "html": "html",
    "css": "css",
    "scss": "css",
}

#: Manifest dosyasi -> paket yoneticisi/build araci.
BUILD_MANIFESTS: dict[str, str] = {
    "pyproject.toml": "python-pyproject",
    "setup.py": "python-setuptools",
    "setup.cfg": "python-setuptools",
    "requirements.txt": "python-requirements",
    "Pipfile": "python-pipenv",
    "poetry.lock": "python-poetry",
    "package.json": "node-npm",
    "pnpm-lock.yaml": "node-pnpm",
    "yarn.lock": "node-yarn",
    "pom.xml": "java-maven",
    "build.gradle": "java-gradle",
    "build.gradle.kts": "java-gradle",
    "Cargo.toml": "rust-cargo",
    "go.mod": "go-modules",
    "composer.json": "php-composer",
    "Gemfile": "ruby-bundler",
    "CMakeLists.txt": "cmake",
    "Makefile": "make",
}

#: Bagimlilik adi deseni -> framework kimligi.
FRAMEWORK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^fastapi$", "fastapi"),
    (r"^django$", "django"),
    (r"^flask$", "flask"),
    (r"^starlette$", "starlette"),
    (r"^sqlalchemy$", "sqlalchemy"),
    (r"^pydantic$", "pydantic"),
    (r"^celery$", "celery"),
    (r"^react$", "react"),
    (r"^next$", "nextjs"),
    (r"^vue$", "vue"),
    (r"^@angular/core$", "angular"),
    (r"^svelte$", "svelte"),
    (r"^express$", "express"),
    (r"^nestjs$|^@nestjs/core$", "nestjs"),
    (r"^spring-boot-starter", "spring-boot"),
    (r"^quarkus", "quarkus"),
    (r"^micronaut", "micronaut"),
    (r"^actix-web$", "actix-web"),
    (r"^axum$", "axum"),
    (r"^gin-gonic/gin$", "gin"),
)

#: Bagimlilik adi deseni -> test cercevesi.
TEST_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^pytest$", "pytest"),
    (r"^unittest2$", "unittest"),
    (r"^tox$", "tox"),
    (r"^jest$", "jest"),
    (r"^vitest$", "vitest"),
    (r"^mocha$", "mocha"),
    (r"^cypress$", "cypress"),
    (r"^playwright$|^@playwright/test$", "playwright"),
    (r"^junit", "junit"),
    (r"^testng$", "testng"),
    (r"^mockito", "mockito"),
)

#: Bagimlilik adi deseni -> veritabani teknolojisi.
DATABASE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^psycopg", "postgresql"),
    (r"^asyncpg$", "postgresql"),
    (r"^pg$", "postgresql"),
    (r"^postgresql$", "postgresql"),
    (r"^cx[-_]oracle$|^oracledb$", "oracle"),
    (r"^ojdbc", "oracle"),
    (r"^mysqlclient$|^pymysql$|^mysql2$", "mysql"),
    (r"^sqlite3$|^aiosqlite$", "sqlite"),
    (r"^pymongo$|^mongoose$", "mongodb"),
    (r"^redis$|^ioredis$", "redis"),
    (r"^pgvector$", "pgvector"),
)

#: Kalite araci yapilandirma dosyalari.
QUALITY_FILES: dict[str, str] = {
    ".ruff.toml": "ruff",
    "ruff.toml": "ruff",
    ".flake8": "flake8",
    ".eslintrc": "eslint",
    ".eslintrc.json": "eslint",
    ".eslintrc.js": "eslint",
    "eslint.config.js": "eslint",
    ".prettierrc": "prettier",
    "checkstyle.xml": "checkstyle",
    ".pre-commit-config.yaml": "pre-commit",
    "mypy.ini": "mypy",
    ".editorconfig": "editorconfig",
}

#: Guvenlik araci yapilandirma dosyalari.
SECURITY_FILES: dict[str, str] = {
    ".bandit": "bandit",
    "bandit.yaml": "bandit",
    ".semgrep.yml": "semgrep",
    "semgrep.yml": "semgrep",
    "trivy.yaml": "trivy",
    ".snyk": "snyk",
    ".gitleaks.toml": "gitleaks",
}

#: Surekli entegrasyon isaretleri (yol oneki -> kimlik).
CI_MARKERS: tuple[tuple[str, str], ...] = (
    (".github/workflows/", "github-actions"),
    (".gitlab-ci.yml", "gitlab-ci"),
    ("Jenkinsfile", "jenkins"),
    ("azure-pipelines.yml", "azure-pipelines"),
    (".circleci/config.yml", "circleci"),
)

#: Konteyner isaretleri.
CONTAINER_FILES: tuple[str, ...] = (
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yaml",
    "compose.yml",
)

#: Bir manifest dosyasindan en fazla okunacak bayt.
MAX_MANIFEST_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class Detection:
    """Tek bir tespit ve kaniti."""

    identifier: str
    evidence_path: str
    evidence_kind: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "evidence_path": self.evidence_path,
            "evidence_kind": self.evidence_kind,
        }


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """Deterministik proje profili."""

    generator_version: str
    languages: tuple[tuple[str, int], ...]
    build_systems: tuple[Detection, ...]
    frameworks: tuple[Detection, ...]
    test_frameworks: tuple[Detection, ...]
    databases: tuple[Detection, ...]
    quality_tools: tuple[Detection, ...]
    security_tools: tuple[Detection, ...]
    continuous_integration: tuple[Detection, ...]
    containers: tuple[Detection, ...]
    modules: tuple[str, ...]
    file_count: int
    total_bytes: int

    def body(self) -> dict[str, Any]:
        """Digest hesaplanan govde."""
        return {
            "generator_version": self.generator_version,
            "languages": [{"language": name, "files": count} for name, count in self.languages],
            "build_systems": [item.as_dict() for item in self.build_systems],
            "frameworks": [item.as_dict() for item in self.frameworks],
            "test_frameworks": [item.as_dict() for item in self.test_frameworks],
            "databases": [item.as_dict() for item in self.databases],
            "quality_tools": [item.as_dict() for item in self.quality_tools],
            "security_tools": [item.as_dict() for item in self.security_tools],
            "continuous_integration": [item.as_dict() for item in self.continuous_integration],
            "containers": [item.as_dict() for item in self.containers],
            "modules": list(self.modules),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }

    @property
    def digest(self) -> str:
        return digest(self.body())

    @property
    def primary_language(self) -> str | None:
        return self.languages[0][0] if self.languages else None

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"profile_digest": self.digest}


def _read_text(root: Path, relative: str) -> str | None:
    path = root / relative
    try:
        if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - okunamayan manifest yoksayilir
        return None


def _dependency_names(root: Path, relative: str) -> tuple[str, ...]:
    """Manifest dosyasindan bagimlilik adlarini cikarir."""
    text = _read_text(root, relative)
    if text is None:
        return ()
    name = Path(relative).name
    if name == "pyproject.toml":
        return _python_pyproject_dependencies(text)
    if name == "requirements.txt":
        return _requirements_dependencies(text)
    if name == "package.json":
        return _package_json_dependencies(text)
    if name in {"pom.xml", "build.gradle", "build.gradle.kts"}:
        return _jvm_dependencies(text)
    if name == "Cargo.toml":
        return _cargo_dependencies(text)
    if name == "go.mod":
        return _go_dependencies(text)
    return ()


def _python_pyproject_dependencies(text: str) -> tuple[str, ...]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ()
    names: list[str] = []
    project = document.get("project", {})
    for entry in project.get("dependencies", []) or []:
        names.append(_requirement_name(str(entry)))
    optional = project.get("optional-dependencies", {}) or {}
    for group in optional.values():
        for entry in group or []:
            names.append(_requirement_name(str(entry)))
    tool = document.get("tool", {})
    poetry = tool.get("poetry", {}) if isinstance(tool, dict) else {}
    for key in ("dependencies", "dev-dependencies"):
        for entry in poetry.get(key, {}) or {}:
            names.append(_requirement_name(str(entry)))
    if isinstance(tool, dict):
        names.extend(f"tool:{key}" for key in tool)
    return tuple(names)


def _requirements_dependencies(text: str) -> tuple[str, ...]:
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        names.append(_requirement_name(stripped))
    return tuple(names)


def _requirement_name(entry: str) -> str:
    return re.split(r"[\s\[<>=!~;]", entry.strip(), maxsplit=1)[0].strip().lower()


def _package_json_dependencies(text: str) -> tuple[str, ...]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return ()
    names: list[str] = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = document.get(key)
        if isinstance(section, dict):
            names.extend(str(name).lower() for name in section)
    return tuple(names)


def _jvm_dependencies(text: str) -> tuple[str, ...]:
    names = [match.lower() for match in re.findall(r"<artifactId>([^<]+)</artifactId>", text)]
    names.extend(
        match.lower()
        for match in re.findall(r"""['"]([a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+)(?::[^'"]*)?['"]""", text)
    )
    expanded: list[str] = []
    for name in names:
        expanded.append(name)
        if ":" in name:
            expanded.append(name.split(":")[-1])
    return tuple(expanded)


def _cargo_dependencies(text: str) -> tuple[str, ...]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ()
    names: list[str] = []
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        section = document.get(key)
        if isinstance(section, dict):
            names.extend(str(name).lower() for name in section)
    return tuple(names)


def _go_dependencies(text: str) -> tuple[str, ...]:
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r"^(?:require\s+)?([a-z0-9./_-]+)\s+v", stripped)
        if match:
            module = match.group(1).lower()
            names.append(module)
            names.append("/".join(module.split("/")[-2:]))
    return tuple(names)


def _match_patterns(
    names: Iterable[str], patterns: tuple[tuple[str, str], ...], evidence_path: str
) -> list[Detection]:
    found: dict[str, Detection] = {}
    for name in names:
        for expression, identifier in patterns:
            if re.search(expression, name) and identifier not in found:
                found[identifier] = Detection(
                    identifier=identifier,
                    evidence_path=evidence_path,
                    evidence_kind="dependency",
                )
    return list(found.values())


def _sorted_detections(detections: Iterable[Detection]) -> tuple[Detection, ...]:
    unique: dict[str, Detection] = {}
    for detection in detections:
        unique.setdefault(detection.identifier, detection)
    return tuple(sorted(unique.values(), key=lambda item: item.identifier))


def _top_level_modules(report: DiscoveryReport) -> tuple[str, ...]:
    modules: set[str] = set()
    for item in report.files:
        head, separator, _ = item.relative_path.partition("/")
        if separator and not head.startswith("."):
            modules.add(head)
    return tuple(sorted(modules))


def _language_counts(report: DiscoveryReport) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for item in report.files:
        language = LANGUAGE_BY_EXTENSION.get(item.extension)
        if language is None:
            continue
        counts[language] = counts.get(language, 0) + 1
    # Once dosya sayisina, esitlikte ada gore sirala: sonuc deterministik olur.
    return tuple(sorted(counts.items(), key=lambda entry: (-entry[1], entry[0])))


def build_profile(root: Path, report: DiscoveryReport) -> CapabilityProfile:
    """Kesif raporundan deterministik capability profili uretir."""
    present = {item.relative_path for item in report.files}
    names_by_path = {Path(path).name: path for path in sorted(present)}

    build_systems: list[Detection] = []
    frameworks: list[Detection] = []
    test_frameworks: list[Detection] = []
    databases: list[Detection] = []

    for manifest_name, build_id in BUILD_MANIFESTS.items():
        path = names_by_path.get(manifest_name)
        if path is None:
            continue
        build_systems.append(
            Detection(identifier=build_id, evidence_path=path, evidence_kind="manifest")
        )
        dependencies = _dependency_names(root, path)
        frameworks.extend(_match_patterns(dependencies, FRAMEWORK_PATTERNS, path))
        test_frameworks.extend(_match_patterns(dependencies, TEST_PATTERNS, path))
        databases.extend(_match_patterns(dependencies, DATABASE_PATTERNS, path))

    quality_tools = [
        Detection(identifier=tool, evidence_path=names_by_path[name], evidence_kind="config")
        for name, tool in QUALITY_FILES.items()
        if name in names_by_path
    ]
    security_tools = [
        Detection(identifier=tool, evidence_path=names_by_path[name], evidence_kind="config")
        for name, tool in SECURITY_FILES.items()
        if name in names_by_path
    ]

    continuous_integration = [
        Detection(identifier=identifier, evidence_path=path, evidence_kind="ci-config")
        for marker, identifier in CI_MARKERS
        for path in sorted(present)
        if path == marker or path.startswith(marker)
    ]
    containers = [
        Detection(identifier="container", evidence_path=names_by_path[name], evidence_kind="config")
        for name in CONTAINER_FILES
        if name in names_by_path
    ]

    # SQL ve PL/SQL dosyalari veritabani kullanimina dogrudan kanittir.
    languages = _language_counts(report)
    language_names = {name for name, _ in languages}
    if "plsql" in language_names:
        databases.append(
            Detection(identifier="oracle", evidence_path="*.pks/*.pkb", evidence_kind="source")
        )

    return CapabilityProfile(
        generator_version=PROFILER_VERSION,
        languages=languages,
        build_systems=_sorted_detections(build_systems),
        frameworks=_sorted_detections(frameworks),
        test_frameworks=_sorted_detections(test_frameworks),
        databases=_sorted_detections(databases),
        quality_tools=_sorted_detections(quality_tools),
        security_tools=_sorted_detections(security_tools),
        continuous_integration=_sorted_detections(continuous_integration),
        containers=_sorted_detections(containers),
        modules=_top_level_modules(report),
        file_count=report.file_count,
        total_bytes=report.total_bytes,
    )


def profile_from_mapping(document: Mapping[str, Any]) -> CapabilityProfile:
    """Kaydedilmis profil belgesini nesneye cevirir."""

    def detections(key: str) -> tuple[Detection, ...]:
        return tuple(
            Detection(
                identifier=item["id"],
                evidence_path=item["evidence_path"],
                evidence_kind=item["evidence_kind"],
            )
            for item in document.get(key, [])
        )

    return CapabilityProfile(
        generator_version=str(document["generator_version"]),
        languages=tuple(
            (item["language"], int(item["files"])) for item in document.get("languages", [])
        ),
        build_systems=detections("build_systems"),
        frameworks=detections("frameworks"),
        test_frameworks=detections("test_frameworks"),
        databases=detections("databases"),
        quality_tools=detections("quality_tools"),
        security_tools=detections("security_tools"),
        continuous_integration=detections("continuous_integration"),
        containers=detections("containers"),
        modules=tuple(document.get("modules", [])),
        file_count=int(document["file_count"]),
        total_bytes=int(document["total_bytes"]),
    )
