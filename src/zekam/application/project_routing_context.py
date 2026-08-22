"""Secret-free, source-bound project context for layered model routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.capability_profile import CapabilityProfile
from zekam.application.source_discovery import DiscoveryReport
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

ROUTING_CONTEXT_GENERATOR_VERSION = "zekam-project-routing-context/v1"

_DEPENDENCY_MANIFESTS = frozenset(
    {
        "build.gradle",
        "build.gradle.kts",
        "cargo.toml",
        "go.mod",
        "package.json",
        "pom.xml",
        "pyproject.toml",
        "requirements.txt",
    }
)
_LOCK_FILES = frozenset(
    {
        "cargo.lock",
        "gradle.lockfile",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)
_RULE_FILES = frozenset(
    {
        ".editorconfig",
        "agents.md",
        "claude.md",
        "codex.md",
        "contributing.md",
        "00_basla.md",
        "devam_protokolu.md",
        "eslint.config.js",
        "eslint.config.mjs",
        "nihai_uygulama_promptu.md",
        "prettier.config.js",
        "ruff.toml",
    }
)


def _basename(relative_path: str) -> str:
    return relative_path.rsplit("/", 1)[-1].casefold()


def _is_architecture_path(relative_path: str) -> bool:
    normalized = relative_path.casefold()
    basename = _basename(normalized)
    return (
        basename in {"architecture.md", "design.md", "readme.md"}
        or normalized.startswith(("adr/", "docs/adr/", "docs/architecture/"))
        or "/architecture/" in normalized
    )


def _selected_file_evidence(report: DiscoveryReport, predicate: Any) -> tuple[dict[str, str], ...]:
    return tuple(
        {"path": item.relative_path, "digest": item.content_digest}
        for item in report.files
        if predicate(item.relative_path)
    )


@dataclass(frozen=True, slots=True)
class ProjectRoutingEvidence:
    """Digest-only project context; source content is intentionally absent."""

    project_id: UUID
    source_revision_id: UUID
    source_revision: str
    tree_digest: str
    capability_profile_digest: str
    dependency_set_digest: str
    dependency_lock_digest: str
    framework_set_digest: str
    technology_profile_digest: str
    architecture_digest: str
    rule_set_digest: str
    project_suite_digest: str
    generator_version: str = ROUTING_CONTEXT_GENERATOR_VERSION

    def __post_init__(self) -> None:
        if not self.source_revision.strip() or len(self.source_revision) > 256:
            raise ValidationFailed("Routing source revision bos veya fazla uzun olamaz")
        for value in (
            self.tree_digest,
            self.capability_profile_digest,
            self.dependency_set_digest,
            self.dependency_lock_digest,
            self.framework_set_digest,
            self.technology_profile_digest,
            self.architecture_digest,
            self.rule_set_digest,
            self.project_suite_digest,
        ):
            parse_digest(value)

    def body(self) -> dict[str, Any]:
        return {
            "schema": self.generator_version,
            "project_id": str(self.project_id),
            "source_revision_id": str(self.source_revision_id),
            "source_revision": self.source_revision,
            "tree_digest": self.tree_digest,
            "capability_profile_digest": self.capability_profile_digest,
            "dependency_set_digest": self.dependency_set_digest,
            "dependency_lock_digest": self.dependency_lock_digest,
            "framework_set_digest": self.framework_set_digest,
            "technology_profile_digest": self.technology_profile_digest,
            "architecture_digest": self.architecture_digest,
            "rule_set_digest": self.rule_set_digest,
            "project_suite_digest": self.project_suite_digest,
        }

    @property
    def context_digest(self) -> str:
        return digest(self.body())

    def sanitized(self) -> dict[str, Any]:
        return self.body() | {"context_digest": self.context_digest, "source_content": False}


def build_project_routing_evidence(
    *,
    project_id: UUID,
    source_revision_id: UUID,
    source_revision: str,
    report: DiscoveryReport,
    profile: CapabilityProfile,
    workloads: tuple[str, ...],
    project_suite_digest: str,
    roles: tuple[str, ...] = ("implementer", "reviewer", "researcher", "verifier"),
) -> ProjectRoutingEvidence:
    """Build a deterministic context that becomes stale on relevant source drift."""

    if report.truncated:
        raise PolicyViolation("Eksik source discovery routing context uretemez")
    if report.tree_digest != digest(
        [{"path": item.relative_path, "digest": item.content_digest} for item in report.files]
    ) and not report.tree_digest.startswith("sha256:"):
        # Existing discovery implementations use a streaming digest, so only malformed
        # values are rejected here; exact currentness is checked against SourceRevision.
        raise PolicyViolation("Routing discovery tree digest gecersiz")
    parse_digest(report.tree_digest)
    normalized_workloads = tuple(
        sorted({item.strip().casefold() for item in workloads if item.strip()})
    )
    normalized_roles = tuple(sorted({item.strip().casefold() for item in roles if item.strip()}))
    if not normalized_workloads or not normalized_roles:
        raise ValidationFailed("Project routing workload ve rol seti bos olamaz")
    parse_digest(project_suite_digest)

    dependencies = _selected_file_evidence(
        report, lambda path: _basename(path) in _DEPENDENCY_MANIFESTS
    )
    locks = _selected_file_evidence(report, lambda path: _basename(path) in _LOCK_FILES)
    rules = _selected_file_evidence(report, lambda path: _basename(path) in _RULE_FILES)
    architecture = _selected_file_evidence(report, _is_architecture_path)
    evidence_digest_by_path = {item.relative_path: item.content_digest for item in report.files}
    frameworks = tuple(
        {
            "id": item.identifier,
            "evidence_path": item.evidence_path,
            "evidence_digest": evidence_digest_by_path.get(item.evidence_path),
        }
        for item in (*profile.frameworks, *profile.test_frameworks)
    )
    technology = {
        "languages": list(profile.languages),
        "build_systems": [item.identifier for item in profile.build_systems],
        "frameworks": [item["id"] for item in frameworks],
        "databases": [item.identifier for item in profile.databases],
        "quality_tools": [item.identifier for item in profile.quality_tools],
        "modules": list(profile.modules),
    }
    return ProjectRoutingEvidence(
        project_id=project_id,
        source_revision_id=source_revision_id,
        source_revision=source_revision,
        tree_digest=report.tree_digest,
        capability_profile_digest=profile.digest,
        dependency_set_digest=digest(list(dependencies)),
        dependency_lock_digest=digest(list(locks)),
        framework_set_digest=digest(list(frameworks)),
        technology_profile_digest=digest(technology),
        architecture_digest=digest(list(architecture)),
        rule_set_digest=digest(list(rules)),
        project_suite_digest=project_suite_digest,
    )
