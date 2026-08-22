"""Reviewed project set and capability-derived workload routing targets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zekam.application.capability_profile import CapabilityProfile
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

TARGET_SCHEMA = "zekam-project-routing-targets/v1"
REQUIRED_PROJECTS = frozenset(
    {
        "gpu-fusion",
        "plsql-java-transformer",
        "plsql-test-sync",
        "utplsql",
        "schema-compare-platform",
        "schema-transform-platform",
        "sky-microservis",
        "sky-ui",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectRoutingTargets:
    projects: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.projects or len(self.projects) != len(set(self.projects)):
            raise ValidationFailed("Project routing target listesi bos veya tekrarli olamaz")
        if any(not value or value != value.casefold() for value in self.projects):
            raise ValidationFailed("Project routing slug degerleri normalize olmali")
        if frozenset(self.projects) != REQUIRED_PROJECTS:
            raise ValidationFailed("Project routing target seti reviewed exact sekiz olmali")

    @property
    def target_digest(self) -> str:
        return digest({"schema": TARGET_SCHEMA, "projects": list(self.projects)})

    def sanitized(self) -> dict[str, Any]:
        return {
            "schema": TARGET_SCHEMA,
            "projects": list(self.projects),
            "target_count": len(self.projects),
            "target_digest": self.target_digest,
        }


def default_target_file() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "project_routing_targets.yaml"


def load_project_routing_targets(path: Path | None = None) -> ProjectRoutingTargets:
    candidate = path or default_target_file()
    if candidate.is_symlink():
        raise PolicyViolation("Project routing target dosyasi symlink olamaz")
    target = candidate.resolve(strict=True)
    if not target.is_file() or target.stat().st_size > 64 * 1024:
        raise PolicyViolation("Project routing target dosyasi guvenli regular file olmali")
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema", "projects"}:
        raise ValidationFailed("Project routing target semasi exact olmali")
    if document["schema"] != TARGET_SCHEMA or not isinstance(document["projects"], list):
        raise ValidationFailed("Project routing target schema/version gecersiz")
    projects = tuple(str(item).strip().casefold() for item in document["projects"])
    return ProjectRoutingTargets(projects=projects)


def workloads_for_profile(profile: CapabilityProfile) -> tuple[str, ...]:
    """Derive workload labels from canonical capability evidence, never from a guess."""

    values = {"project"}
    values.update(name.casefold() for name, _ in profile.languages)
    values.update(item.identifier.casefold() for item in profile.frameworks)
    values.update(item.identifier.casefold() for item in profile.test_frameworks)
    values.update(item.identifier.casefold() for item in profile.databases)
    return tuple(sorted(values))
