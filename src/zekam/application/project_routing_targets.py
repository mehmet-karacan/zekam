"""Reviewed project set and capability-derived workload routing targets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zekam.application.capability_profile import CapabilityProfile
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_routing import AgentRole, RouteCapabilityRequirements

TARGET_SCHEMA = "zekam-project-routing-targets/v2"
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
    capability_requirements: tuple[tuple[AgentRole, RouteCapabilityRequirements], ...]
    capability_evidence_roles: tuple[tuple[AgentRole, AgentRole], ...]

    def __post_init__(self) -> None:
        if not self.projects or len(self.projects) != len(set(self.projects)):
            raise ValidationFailed("Project routing target listesi bos veya tekrarli olamaz")
        if any(not value or value != value.casefold() for value in self.projects):
            raise ValidationFailed("Project routing slug degerleri normalize olmali")
        if frozenset(self.projects) != REQUIRED_PROJECTS:
            raise ValidationFailed("Project routing target seti reviewed exact sekiz olmali")
        roles = tuple(role for role, _ in self.capability_requirements)
        if len(roles) != len(set(roles)) or frozenset(roles) != frozenset(AgentRole):
            raise ValidationFailed("Project routing capability rolleri exact olmali")
        if any(
            not requirement.required_dimensions for _, requirement in self.capability_requirements
        ):
            raise ValidationFailed("Project routing capability gereksinimi bos olamaz")
        evidence_roles = tuple(role for role, _ in self.capability_evidence_roles)
        if len(evidence_roles) != len(set(evidence_roles)) or frozenset(
            evidence_roles
        ) != frozenset(AgentRole):
            raise ValidationFailed("Project routing capability evidence rolleri exact olmali")

    def requirements_for(self, role: AgentRole) -> RouteCapabilityRequirements:
        return dict(self.capability_requirements)[role]

    def evidence_role_for(self, role: AgentRole) -> AgentRole:
        return dict(self.capability_evidence_roles)[role]

    @property
    def target_digest(self) -> str:
        return digest(
            {
                "schema": TARGET_SCHEMA,
                "projects": list(self.projects),
                "capability_requirements": {
                    role.value: {
                        "evidence_role": self.evidence_role_for(role).value,
                        **requirement.as_dict(),
                    }
                    for role, requirement in self.capability_requirements
                },
            }
        )

    def sanitized(self) -> dict[str, Any]:
        return {
            "schema": TARGET_SCHEMA,
            "projects": list(self.projects),
            "target_count": len(self.projects),
            "capability_requirements": {
                role.value: {
                    "evidence_role": self.evidence_role_for(role).value,
                    **requirement.as_dict(),
                }
                for role, requirement in self.capability_requirements
            },
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
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "projects",
        "capability_requirements",
    }:
        raise ValidationFailed("Project routing target semasi exact olmali")
    if (
        document["schema"] != TARGET_SCHEMA
        or not isinstance(document["projects"], list)
        or not isinstance(document["capability_requirements"], dict)
    ):
        raise ValidationFailed("Project routing target schema/version gecersiz")
    projects = tuple(str(item).strip().casefold() for item in document["projects"])
    requirement_document = document["capability_requirements"]
    if set(requirement_document) != {role.value for role in AgentRole}:
        raise ValidationFailed("Project routing capability rolleri exact olmali")
    expected_fields = {"evidence_role", *RouteCapabilityRequirements().as_dict()}
    requirements: list[tuple[AgentRole, RouteCapabilityRequirements]] = []
    evidence_roles: list[tuple[AgentRole, AgentRole]] = []
    for role in AgentRole:
        value = requirement_document[role.value]
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise ValidationFailed("Project routing capability semasi exact olmali")
        try:
            evidence_role = AgentRole(str(value["evidence_role"]))
            requirement = RouteCapabilityRequirements(
                **{key: item for key, item in value.items() if key != "evidence_role"}
            )
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("Project routing capability degerleri gecersiz") from exc
        requirements.append((role, requirement))
        evidence_roles.append((role, evidence_role))
    return ProjectRoutingTargets(
        projects=projects,
        capability_requirements=tuple(requirements),
        capability_evidence_roles=tuple(evidence_roles),
    )


def workloads_for_profile(profile: CapabilityProfile) -> tuple[str, ...]:
    """Derive workload labels from canonical capability evidence, never from a guess."""

    values = {"project"}
    values.update(name.casefold() for name, _ in profile.languages)
    values.update(item.identifier.casefold() for item in profile.frameworks)
    values.update(item.identifier.casefold() for item in profile.test_frameworks)
    values.update(item.identifier.casefold() for item in profile.databases)
    return tuple(sorted(values))
