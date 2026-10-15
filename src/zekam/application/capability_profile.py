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
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from zekam.application.bounded_file import read_bounded_bytes
from zekam.application.source_discovery import DiscoveryReport
from zekam.domain.canonical import digest
from zekam.domain.client_integration import ClientIntegrationId
from zekam.domain.errors import ValidationFailed

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
        return read_bounded_bytes(path, max_bytes=MAX_MANIFEST_BYTES).decode(
            "utf-8", errors="replace"
        )
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


# ---------------------------------------------------------------------------
# Surum bazli, istemci-bazli native yetenek destegi matrisi (WP-02.02)
#
# Her istemci icin metadata/instruction/hook/MCP/shell/subagent destegi AYRI AYRI,
# surum + kanit seviyesi + destek siniri olarak kaydedilir. Destek yoksa
# manual komut ezberi degil, acik destek siniri ve kurulabilir adapter plani
# verilir. `observed` kaniti yoksa `ready` iddia edilmez (NL-039 / GAP-07).
#
# Kanit duzeni: matristeki surum, Zekam tarafi GENERATOR (instruction section /
# hook / MCP projection / agent bootstrap / proje-yerel ince giris) kodunun
# review kanitini tasiyan surumudur (`version_basis="reviewed-generator"`).
# `host_present`/`host_version` ise BU hostta gozlenen native CLI kurulumunun
# acik kanitidir ve generator surumundan ayri tutulur. `ready` ancak
# `observed` kanitla; `observed` kaniti yalniz host kanitiyla tasinir.
# Hostta kurulu olmayan istemci icin generator kanitli hucreler `declared`
# kalir, hicbir hucre `ready`+`observed` iddia edilmez.
# ---------------------------------------------------------------------------

CLIENT_CAPABILITY_MATRIX_SCHEMA: Final = "zekam-client-capability-matrix/v2"

#: WP-02.02 icin surum bazli kaydi zorunlu olan yetenek boyutlari.
SUPPORTED_CAPABILITIES: Final = (
    "metadata",
    "instruction",
    "hook",
    "mcp",
    "shell",
    "subagent",
)

class CapabilityStatus(StrEnum):
    """Bir yetenek boyutundaki destek durumu; `ready` ancak `observed` kanitla."""

    NOT_SUPPORTED = "not-supported"
    DECLARED = "declared"
    PARTIAL = "partial"
    READY = "ready"

    @classmethod
    def all(cls) -> frozenset[str]:
        return frozenset(item.value for item in cls)


@dataclass(frozen=True, slots=True)
class CapabilitySupport:
    """Tek istemci icin tek yetenek boyutunun surum/kanit/sinir kaydi."""

    capability: str
    status: str
    version: str
    evidence_level: str  # "observed" | "declared" | "unobserved"
    support_limit: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "status": self.status,
            "version": self.version,
            "evidence_level": self.evidence_level,
            "support_limit": self.support_limit,
        }


@dataclass(frozen=True, slots=True)
class ClientCapabilityProfile:
    """Tek desteklenen istemcinin surum bazli yetenek matris satiri.

    `version`/`version_basis` generator (reviewed) surumudur. `host_present` ve
    `host_version` bu hosttaki native CLI kurulumunun gozlenen kanitidir ve
    generator surumundan BAYRAK ve surum olarak ayri tutulur.
    """

    client: str
    version: str
    version_basis: str
    capabilities: tuple[CapabilitySupport, ...]
    adapter_plan: tuple[str, ...]
    support_limit: str
    host_present: bool = False
    host_version: str | None = None

    def body(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "version": self.version,
            "version_basis": self.version_basis,
            "host_present": self.host_present,
            "host_version": self.host_version,
            "capabilities": [item.as_dict() for item in self.capabilities],
            "adapter_plan": list(self.adapter_plan),
            "support_limit": self.support_limit,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body()


@dataclass(frozen=True, slots=True)
class ClientCapabilityMatrix:
    """Surum bazli, istemci-bazli native yetenek destegi matrisi.

    `observed_hosts` host kaniti: bu hostta gozlenen native CLI kimlikleri ve
    surumlari. `observed_hosts` disinda hic istemci `observed` kaniti tasiyamaz.
    """

    schema: str
    generated_from: str
    clients: tuple[ClientCapabilityProfile, ...]
    observed_hosts: tuple[str, ...] = ()
    read_only: bool = True
    grants_authority: bool = False

    def body(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_from": self.generated_from,
            "supported_capabilities": list(SUPPORTED_CAPABILITIES),
            "observed_hosts": list(self.observed_hosts),
            "clients": [item.body() for item in self.clients],
            "read_only": self.read_only,
            "grants_authority": self.grants_authority,
        }

    @property
    def matrix_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"matrix_digest": self.matrix_digest}


def _cap(
    capability: str,
    status: CapabilityStatus,
    version: str,
    evidence_level: str,
    support_limit: str,
) -> CapabilitySupport:
    return CapabilitySupport(
        capability=capability,
        status=status.value,
        version=version,
        evidence_level=evidence_level,
        support_limit=support_limit,
    )


def _client_profile(
    client: ClientIntegrationId,
    *,
    version: str,
    version_basis: str,
    metadata: CapabilitySupport,
    instruction: CapabilitySupport,
    hook: CapabilitySupport,
    mcp: CapabilitySupport,
    shell: CapabilitySupport,
    subagent: CapabilitySupport,
    adapter_plan: tuple[str, ...],
    support_limit: str,
) -> ClientCapabilityProfile:
    capabilities = (metadata, instruction, hook, mcp, shell, subagent)
    assert [item.capability for item in capabilities] == list(SUPPORTED_CAPABILITIES)
    return ClientCapabilityProfile(
        client=client.value,
        version=version,
        version_basis=version_basis,
        capabilities=capabilities,
        adapter_plan=adapter_plan,
        support_limit=support_limit,
    )


def _downgrade_without_host(profile: ClientCapabilityProfile) -> ClientCapabilityProfile:
    """Host kaniti yoksa generator kanitli hucreleri `declared`e duzeltir.

    `ready` ancak `observed` kanitla ve `observed` kaniti yalniz host kanitiyla
    tasinir; hostta kurulu olmayan istemci icin generator kanitli hucreler
    `declared`/`unobserved` kalir ve acik destek siniri korunur.
    """

    capabilities = tuple(
        CapabilitySupport(
            capability=item.capability,
            status=CapabilityStatus.DECLARED.value,
            version=item.version,
            evidence_level="unobserved",
            support_limit=item.support_limit,
        )
        if item.status == CapabilityStatus.READY.value and item.evidence_level == "observed"
        else item
        for item in profile.capabilities
    )
    return ClientCapabilityProfile(
        client=profile.client,
        version=profile.version,
        version_basis=profile.version_basis,
        capabilities=capabilities,
        adapter_plan=profile.adapter_plan,
        support_limit=profile.support_limit,
    )


def client_capability_matrix(
    *,
    observed_hosts: Mapping[str, str] | None = None,
) -> ClientCapabilityMatrix:
    """Desteklenen dort istemci icin surum bazli yetenek destegi matrisi.

    Durumlar Zekam generator'larinin review kanitina baglidir; generator surumu
    (`version_basis="reviewed-generator"`) bu hosttaki native kurulumdan ayri
    kaydedilir (`host_present`/`host_version`). `observed_hosts` (native CLI
    kimligi -> gozlenen surum) binary varligi kanitidir. Binary varligi native
    davranisin basarili oldugunu kanitlamaz; generator hucreleri acceptance
    kosusuna kadar `declared`/`unobserved` kalir (NL-039 / GAP-07).
    """

    if observed_hosts is None:
        observed_input: Mapping[Any, Any] = {}
    elif isinstance(observed_hosts, Mapping):
        observed_input = observed_hosts
    else:
        raise ValidationFailed("Gozlenen host matrisi mapping olmali")
    observed: dict[ClientIntegrationId, str] = {}
    for client_id, version in observed_input.items():
        if not isinstance(client_id, str):
            raise ValidationFailed("Gozlenen host istemci kimligi metin olmali")
        try:
            identity = ClientIntegrationId(client_id)
        except ValueError as exc:
            raise ValidationFailed("Gozlenen host istemci kimligi desteklenmiyor") from exc
        if not isinstance(version, str) or not version.strip():
            raise ValidationFailed("Gozlenen host surumu bos olamaz")
        observed[identity] = version.strip()

    # -- OpenCode ----------------------------------------------------------
    opencode = _client_profile(
        ClientIntegrationId.OPENCODE,
        version="managed-instructions/v1+agent-bootstrap+plugin-v3",
        version_basis="reviewed-generator",
        metadata=_cap(
            "metadata",
            CapabilityStatus.DECLARED,
            "host-native-cli/v1",
            "declared",
            "istemci kimligi/surumu `host_present`/`host_version` ile ayri raporlanir; "
            "native davranis acceptance kosusuna kadar kanitlanmaz",
        ),
        instruction=_cap(
            "instruction",
            CapabilityStatus.READY,
            "managed-instructions/v1",
            "observed",
            "project-local AGENTS.md + managed global section (digest-owned)",
        ),
        hook=_cap(
            "hook",
            CapabilityStatus.READY,
            "opencode-lifecycle-plugin/v3",
            "observed",
            "lifecycle events (SessionStart/PreCompact/PostCompact/Stop/SessionEnd) via plugin",
        ),
        mcp=_cap(
            "mcp",
            CapabilityStatus.READY,
            "mcp-integrations/opencode-config",
            "observed",
            "opencode.json mcpserver projection; credential'lar kopyalanmaz",
        ),
        shell=_cap(
            "shell",
            CapabilityStatus.DECLARED,
            "host-native-permission/v1",
            "declared",
            "shell `allow` hedefi WP-02.03 host-native policy'ye bagli; buradan garanti verilemez",
        ),
        subagent=_cap(
            "subagent",
            CapabilityStatus.PARTIAL,
            "host-native-task/v1",
            "declared",
            "named agent dispatch host'ta var; Zekam child identity/verifier WP-08'e bagli",
        ),
        adapter_plan=(
            "global bootstrap opt-in: zekam integration sync --scope user",
            "project-local giris: AGENTS.md (dogrudan kesif)",
        ),
        support_limit="global artifact'lar opt-in; project-local giris global kurulum gerektirmez",
    )

    # -- Codex -------------------------------------------------------------
    codex = _client_profile(
        ClientIntegrationId.CODEX,
        version="managed-instructions/v1+hook@0.154.0+config.toml-mcp",
        version_basis="reviewed-generator",
        metadata=_cap(
            "metadata",
            CapabilityStatus.DECLARED,
            "host-native-cli/v1",
            "declared",
            "istemci kimligi/surumu `host_present`/`host_version` ile ayri raporlanir; "
            "native davranis acceptance kosusuna kadar kanitlanmaz",
        ),
        instruction=_cap(
            "instruction",
            CapabilityStatus.READY,
            "managed-instructions/v1",
            "observed",
            "project-local AGENTS.md + managed global section (digest-owned)",
        ),
        hook=_cap(
            "hook",
            CapabilityStatus.READY,
            "hook@0.154.0",
            "observed",
            ".codex/hooks.json managed lifecycle hook (reviewed version)",
        ),
        mcp=_cap(
            "mcp",
            CapabilityStatus.READY,
            "mcp-integrations/codex-config-toml",
            "observed",
            ".codex/config.toml mcpserver projection; credential'lar kopyalanmaz",
        ),
        shell=_cap(
            "shell",
            CapabilityStatus.DECLARED,
            "host-native-permission/v1",
            "declared",
            "shell `allow` hedefi WP-02.03 host-native policy'ye bagli; buradan garanti verilemez",
        ),
        subagent=_cap(
            "subagent",
            CapabilityStatus.NOT_SUPPORTED,
            "host-native",
            "unobserved",
            "Zekam child subagent kimligi/verifier bu host icin kanitlanmadi (WP-08)",
        ),
        adapter_plan=(
            "global bootstrap opt-in: zekam integration sync --scope user",
            "project-local giris: AGENTS.md (dogrudan kesif)",
        ),
        support_limit="global artifact'lar opt-in; project-local giris global kurulum gerektirmez",
    )

    # -- Claude Code -------------------------------------------------------
    # Bu hostta Claude Code kurulu degil: generator surumu `reviewed-generator`
    # kanitla kalir; `observed` kaniti yalniz host kurulumu olursa eklenir.
    claude = _client_profile(
        ClientIntegrationId.CLAUDE_CODE,
        version="managed-instructions/v1+hook@2.1.224+claude.json-mcp",
        version_basis="reviewed-generator",
        metadata=_cap(
            "metadata",
            CapabilityStatus.DECLARED,
            "host-native-cli/v1",
            "declared",
            "istemci kimligi/surumu `host_present`/`host_version` ile ayri raporlanir; "
            "native davranis acceptance kosusuna kadar kanitlanmaz",
        ),
        instruction=_cap(
            "instruction",
            CapabilityStatus.READY,
            "managed-instructions/v1",
            "observed",
            "project-local CLAUDE.md (@AGENTS.md) + managed global section (digest-owned)",
        ),
        hook=_cap(
            "hook",
            CapabilityStatus.READY,
            "hook@2.1.224",
            "observed",
            ".claude/settings.json managed lifecycle hook (reviewed version)",
        ),
        mcp=_cap(
            "mcp",
            CapabilityStatus.READY,
            "mcp-integrations/claude-json",
            "observed",
            ".claude.json mcpserver projection; credential'lar kopyalanmaz",
        ),
        shell=_cap(
            "shell",
            CapabilityStatus.DECLARED,
            "host-native-permission/v1",
            "declared",
            "shell `allow` hedefi WP-02.03 host-native policy'ye bagli; buradan garanti verilemez",
        ),
        subagent=_cap(
            "subagent",
            CapabilityStatus.NOT_SUPPORTED,
            "host-native",
            "unobserved",
            "Zekam child subagent kimligi/verifier bu host icin kanitlanmadi (WP-08)",
        ),
        adapter_plan=(
            "global bootstrap opt-in: zekam integration sync --scope user",
            "project-local giris: CLAUDE.md (@AGENTS.md import)",
        ),
        support_limit="global artifact'lar opt-in; project-local giris global kurulum gerektirmez",
    )

    # -- Gemini CLI --------------------------------------------------------
    # Zekam tarafinda Gemini icin managed global artifact uretilmez; giris yalniz
    # project-local. Bu hostta Gemini kurulu degil: generator surumu `reviewed-generator`
    # kanitla kalir; `observed` kaniti yalniz host kurulumu olursa eklenir.
    gemini = _client_profile(
        ClientIntegrationId.GEMINI,
        version="project-local-entry-only",
        version_basis="reviewed-generator",
        metadata=_cap(
            "metadata",
            CapabilityStatus.DECLARED,
            "host-native-cli/v1",
            "declared",
            "istemci kimligi/surumu `host_present`/`host_version` ile ayri raporlanir; "
            "native davranis acceptance kosusuna kadar kanitlanmaz",
        ),
        instruction=_cap(
            "instruction",
            CapabilityStatus.READY,
            "managed-instructions/v1",
            "observed",
            "project-local GEMINI.md (@AGENTS.md import); managed global artifact yok",
        ),
        hook=_cap(
            "hook",
            CapabilityStatus.NOT_SUPPORTED,
            "host-native",
            "unobserved",
            "Zekam tarafinda Gemini icin managed lifecycle hook uretmez (acik sinir)",
        ),
        mcp=_cap(
            "mcp",
            CapabilityStatus.NOT_SUPPORTED,
            "host-native",
            "unobserved",
            "MCP projection hedefi bu host icin tanimlanmadi; manuel MCP ayari degistirilmez",
        ),
        shell=_cap(
            "shell",
            CapabilityStatus.DECLARED,
            "host-native-permission/v1",
            "declared",
            "shell `allow` hedefi WP-02.03 host-native policy'ye bagli; buradan garanti verilemez",
        ),
        subagent=_cap(
            "subagent",
            CapabilityStatus.NOT_SUPPORTED,
            "host-native",
            "unobserved",
            "Zekam child subagent kimligi/verifier bu host icin kanitlanmadi (WP-08)",
        ),
        adapter_plan=(
            "project-local giris: GEMINI.md (@AGENTS.md import)",
            "global artifact: uretilmez; `gemini: true` yeni global kurulum uretmez",
        ),
        support_limit="yalniz project-local giris; global Zekam artifact'i yok",
    )

    # Binary presence records a host identity, not successful native behavior.
    # Reviewed generator cells remain declared until a separate acceptance run.
    def _with_host(
        profile: ClientCapabilityProfile, client: ClientIntegrationId
    ) -> ClientCapabilityProfile:
        if client not in observed:
            return _downgrade_without_host(profile)
        declared = _downgrade_without_host(profile)
        return ClientCapabilityProfile(
            client=profile.client,
            version=profile.version,
            version_basis=profile.version_basis,
            capabilities=declared.capabilities,
            adapter_plan=profile.adapter_plan,
            support_limit=profile.support_limit,
            host_present=True,
            host_version=observed[client],
        )

    return ClientCapabilityMatrix(
        schema=CLIENT_CAPABILITY_MATRIX_SCHEMA,
        generated_from=PROFILER_VERSION,
        clients=(
            _with_host(opencode, ClientIntegrationId.OPENCODE),
            _with_host(codex, ClientIntegrationId.CODEX),
            _with_host(claude, ClientIntegrationId.CLAUDE_CODE),
            _with_host(gemini, ClientIntegrationId.GEMINI),
        ),
        observed_hosts=tuple(client.value for client in ClientIntegrationId if client in observed),
    )
