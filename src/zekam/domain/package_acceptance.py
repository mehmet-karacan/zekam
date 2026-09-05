"""Shipped package manifest and artifact acceptance contracts."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

PACKAGE_MANIFEST_SCHEMA = "zekam-package-manifest/v3"
ACCEPTANCE_RUN_SCHEMA = "zekam-package-acceptance-run/v1"
VERIFIER_PROVENANCE_SCHEMA = "zekam-package-verifier-provenance/v1"
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)?$")
_CHECK_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")


@dataclass(frozen=True, slots=True)
class PackageManifestV3:
    """Deterministic component inventory embedded in every wheel."""

    version: str
    entrypoints: tuple[str, ...]
    python: str
    local_schema_bundle_digest: str
    package_source_bundle_digest: str
    config_bundle_digest: str
    protocol_schema_digest: str
    agent_template_digest: str
    build_provenance_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, str):
            raise ValidationFailed("Package manifest surumu metin olmali")
        if not _VERSION.fullmatch(self.version):
            raise ValidationFailed("Package manifest surumu semantik olmali")
        if not isinstance(self.entrypoints, tuple) or any(
            not isinstance(item, str) for item in self.entrypoints
        ):
            raise ValidationFailed("Package entrypoint listesi metin tuple olmali")
        if not self.entrypoints or tuple(sorted(set(self.entrypoints))) != self.entrypoints:
            raise ValidationFailed("Package entrypoint listesi unique ve sirali olmali")
        if not isinstance(self.python, str) or not self.python.startswith(">="):
            raise ValidationFailed("Package Python siniri acik minimum istemeli")
        for value in (
            self.local_schema_bundle_digest,
            self.package_source_bundle_digest,
            self.config_bundle_digest,
            self.protocol_schema_digest,
            self.agent_template_digest,
            self.build_provenance_digest,
        ):
            if not isinstance(value, str):
                raise ValidationFailed("Package manifest digest alanlari metin olmali")
            parse_digest(value)

    def body(self) -> dict[str, Any]:
        return {
            "schema": PACKAGE_MANIFEST_SCHEMA,
            "version": self.version,
            "entrypoints": list(self.entrypoints),
            "python": self.python,
            "local_schema_bundle_digest": self.local_schema_bundle_digest,
            "package_source_bundle_digest": self.package_source_bundle_digest,
            "config_bundle_digest": self.config_bundle_digest,
            "protocol_schema_digest": self.protocol_schema_digest,
            "agent_template_digest": self.agent_template_digest,
            "build_provenance_digest": self.build_provenance_digest,
        }

    @property
    def manifest_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def parse(cls, value: Any) -> PackageManifestV3:
        expected = {
            "schema",
            "version",
            "entrypoints",
            "python",
            "local_schema_bundle_digest",
            "package_source_bundle_digest",
            "config_bundle_digest",
            "protocol_schema_digest",
            "agent_template_digest",
            "build_provenance_digest",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValidationFailed("Package manifest exact v3 schema ister")
        scalar_fields = (
            "schema",
            "version",
            "python",
            "local_schema_bundle_digest",
            "package_source_bundle_digest",
            "config_bundle_digest",
            "protocol_schema_digest",
            "agent_template_digest",
            "build_provenance_digest",
        )
        if (
            value["schema"] != PACKAGE_MANIFEST_SCHEMA
            or any(not isinstance(value[field], str) for field in scalar_fields)
            or not isinstance(value["entrypoints"], list)
            or any(not isinstance(item, str) for item in value["entrypoints"])
        ):
            raise ValidationFailed("Package manifest schema/entrypoints gecersiz")
        return cls(
            version=value["version"],
            entrypoints=tuple(value["entrypoints"]),
            python=value["python"],
            local_schema_bundle_digest=value["local_schema_bundle_digest"],
            package_source_bundle_digest=value["package_source_bundle_digest"],
            config_bundle_digest=value["config_bundle_digest"],
            protocol_schema_digest=value["protocol_schema_digest"],
            agent_template_digest=value["agent_template_digest"],
            build_provenance_digest=value["build_provenance_digest"],
        )


class AcceptanceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PackageAcceptanceResult:
    check_id: str
    status: AcceptanceStatus
    command_digest: str
    stdout_digest: str
    stderr_digest: str
    duration_ms: int
    detail: str | None = None

    def __post_init__(self) -> None:
        if not _CHECK_ID.fullmatch(self.check_id):
            raise ValidationFailed("Package acceptance check_id gecersiz")
        for value in (self.command_digest, self.stdout_digest, self.stderr_digest):
            parse_digest(value)
        if self.duration_ms < 0:
            raise ValidationFailed("Package acceptance duration negatif olamaz")
        if self.status is AcceptanceStatus.SKIPPED and not self.detail:
            raise ValidationFailed("Skipped package check gerekce ister")
        if self.detail and len(self.detail) > 256:
            raise ValidationFailed("Package acceptance detail cok uzun")

    def body(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "command_digest": self.command_digest,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }

    @property
    def result_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class PackageVerifierProvenance:
    """Canonical builder/verifier executions bound to immutable agent receipts."""

    builder_assignment_id: UUID
    builder_invocation_id: UUID
    builder_execution_identity: str
    builder_envelope_digest: str
    verifier_assignment_id: UUID
    verifier_invocation_id: UUID
    verifier_execution_identity: str
    verifier_envelope_digest: str
    verifier_source_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.builder_envelope_digest,
            self.verifier_envelope_digest,
            self.verifier_source_digest,
        ):
            parse_digest(value)
        if self.builder_assignment_id == self.verifier_assignment_id:
            raise PolicyViolation("Builder ve verifier assignment ayri olmali")
        if self.builder_invocation_id == self.verifier_invocation_id:
            raise PolicyViolation("Builder ve verifier invocation ayri olmali")
        if self.builder_execution_identity == self.verifier_execution_identity:
            raise PolicyViolation("Builder ve verifier execution identity ayri olmali")
        for value in (self.builder_execution_identity, self.verifier_execution_identity):
            if not value.strip() or len(value) > 256:
                raise ValidationFailed("Package verifier execution identity gecersiz")

    def body(self) -> dict[str, Any]:
        return {
            "schema": VERIFIER_PROVENANCE_SCHEMA,
            "builder_assignment_id": str(self.builder_assignment_id),
            "builder_invocation_id": str(self.builder_invocation_id),
            "builder_execution_identity": self.builder_execution_identity,
            "builder_envelope_digest": self.builder_envelope_digest,
            "verifier_assignment_id": str(self.verifier_assignment_id),
            "verifier_invocation_id": str(self.verifier_invocation_id),
            "verifier_execution_identity": self.verifier_execution_identity,
            "verifier_envelope_digest": self.verifier_envelope_digest,
            "verifier_source_digest": self.verifier_source_digest,
        }

    @property
    def provenance_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class PackageAcceptanceRun:
    id: UUID
    manifest_digest: str
    artifact_digest: str
    artifact_kind: str
    source_revision: str
    suite_digest: str
    platform: str
    python_version: str
    builder_identity: str
    verifier_identity: str
    verifier_provenance: PackageVerifierProvenance
    started_at: dt.datetime
    completed_at: dt.datetime
    results: tuple[PackageAcceptanceResult, ...]
    isolated_environment: bool = True
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.manifest_digest,
            self.artifact_digest,
            self.suite_digest,
        ):
            parse_digest(value)
        if self.artifact_kind not in {"wheel", "sdist", "container"}:
            raise ValidationFailed("Package artifact kind gecersiz")
        for value in (
            self.source_revision,
            self.platform,
            self.python_version,
            self.builder_identity,
            self.verifier_identity,
        ):
            if not value.strip() or len(value) > 256:
                raise ValidationFailed("Package acceptance kimlik alani gecersiz")
        if self.builder_identity == self.verifier_identity:
            raise PolicyViolation("Package acceptance verifier builder'dan bagimsiz olmali")
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValidationFailed("Package acceptance timezone-aware zaman ister")
        if self.completed_at < self.started_at:
            raise ValidationFailed("Package acceptance zaman sirasi gecersiz")
        ids = tuple(item.check_id for item in self.results)
        if not ids or tuple(sorted(set(ids))) != ids:
            raise ValidationFailed("Package acceptance sonuclari unique ve sirali olmali")
        if not self.isolated_environment:
            raise PolicyViolation("Package acceptance yalitilmis environment ister")
        if self.grants_authority:
            raise PolicyViolation("Package acceptance authority tasiyamaz")

    @property
    def status(self) -> AcceptanceStatus:
        if any(item.status is AcceptanceStatus.FAILED for item in self.results):
            return AcceptanceStatus.FAILED
        if any(item.status is AcceptanceStatus.SKIPPED for item in self.results):
            return AcceptanceStatus.SKIPPED
        return AcceptanceStatus.PASSED

    def body(self) -> dict[str, Any]:
        return {
            "schema": ACCEPTANCE_RUN_SCHEMA,
            "id": str(self.id),
            "manifest_digest": self.manifest_digest,
            "artifact_digest": self.artifact_digest,
            "artifact_kind": self.artifact_kind,
            "source_revision": self.source_revision,
            "suite_digest": self.suite_digest,
            "platform": self.platform,
            "python_version": self.python_version,
            "builder_identity": self.builder_identity,
            "verifier_identity": self.verifier_identity,
            "verifier_provenance": self.verifier_provenance.body(),
            "verifier_provenance_digest": self.verifier_provenance_digest,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "status": self.status.value,
            "isolated_environment": True,
            "results": [
                item.body() | {"result_digest": item.result_digest} for item in self.results
            ],
            "grants_authority": False,
        }

    @property
    def run_digest(self) -> str:
        return digest(self.body())

    @property
    def verifier_provenance_digest(self) -> str:
        return self.verifier_provenance.provenance_digest
