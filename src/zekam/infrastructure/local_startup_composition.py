"""Existing-state startup wiring; no bootstrap, provider or installed-hook activation.

Structural lifecycle observations are revalidated, but this composition does not
assert that a particular installed client emitted them. Native activation remains
an explicit, separate gate. Optional retrieval is read-only and provider-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.local_continuity_source_plan import MAX_SOURCE_BYTES, ContinuitySourcePlan
from zekam.application.local_continuity_startup import LocalStartupService, StartupRequest
from zekam.application.local_startup_retrieval import LocalStartupRetrieval
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.local_continuity_decoder import validate_reviewed_control_entry
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_continuity_environment import LocalContinuityEnvironment
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
from zekam.infrastructure.sqlite.local_startup_checkpoint import SQLiteStartupCheckpointSource
from zekam.infrastructure.sqlite.local_startup_notes import SQLiteStartupNoteSource
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


class _BoundedProjectSource(ProjectContinuitySourceResolver):
    def __init__(
        self,
        source: BoundedContinuitySource,
        plan: ContinuitySourcePlan,
        binding: ContinuityBinding,
    ) -> None:
        super().__init__(
            source.root,
            project_id=binding.project_id,
            realm_id=binding.realm_id,
            source_snapshot_id=binding.source_snapshot_id,
            allowed_paths=plan.recipe.allowed_paths,
        )
        self._source, self._plan, self._binding = source, plan, binding

    def read_fragment(self, binding: ContinuityBinding, ref: object) -> str:
        if binding != self._binding:
            raise PolicyViolation("Bounded startup fragment exact binding mismatch")
        if not isinstance(ref, str):
            raise ValidationFailed("Bounded startup source locator required")
        path, separator, locator = ref.partition("#")
        pinned = next((file for file in self._plan.files if file.relative_path == path), None)
        if pinned is None:
            raise PolicyViolation("Bounded startup fragment outside source recipe")
        payload = self._source._read(path, MAX_SOURCE_BYTES)
        if (
            payload is None
            or len(payload) != pinned.size_bytes
            or digest_of_bytes(payload) != pinned.content_digest
        ):
            raise PolicyViolation("Bounded startup full source identity drift")
        # Full-file verification and line extraction share this sole captured buffer.
        text = payload.decode("utf-8")
        if separator:
            match = re.fullmatch(r"L([1-9][0-9]*)-L([1-9][0-9]*)", locator)
            if match is None:
                raise ValidationFailed("Bounded startup line locator malformed")
            first, last = (int(value) for value in match.groups())
            lines = text.splitlines(keepends=True)
            if not 1 <= first <= last <= len(lines):
                raise ValidationFailed("Bounded startup line locator outside captured file")
            text = "".join(lines[first - 1 : last])
        return text


class _CurrentStartupSource(SQLiteStartupSourceResolver):
    """Recheck real source at each resolver call, including the hydration transaction."""

    source_capture: BoundedContinuitySource
    operational: SQLiteOperationalStore

    def preflight(self, binding: ContinuityBinding) -> dict[str, Any]:
        report = super().preflight(binding)
        if report is None:
            raise PolicyViolation("Bounded startup environment required")
        current = self.source_capture.probe(self.operational, binding)
        evidence = {key: value for key, value in report.items() if key != "evidence_digest"} | {
            "schema": "zekam-bounded-startup-environment/v1",
            "environment_evidence_digest": report["evidence_digest"],
            "source_capture_digest": current,
            "source_scope": "explicit-bounded-tracked-files",
            "atomic_filesystem_snapshot": False,
        }
        return evidence | {"evidence_digest": digest(evidence)}


@dataclass(frozen=True, slots=True)
class LocalStartupComposition:
    """Explicit structural-only entry points; all input admission already exists."""

    service: LocalStartupService
    lifecycle: LocalLifecycleContinuity
    sources: SQLiteStartupSourceResolver

    def hydrate(self, request: StartupRequest) -> dict[str, Any]:
        result = self.service.hydrate(request)
        return result | {
            "client_evidence": "reviewed-structural-observations-only",
            "installed_client_lifecycle_proven": False,
            "hook_activation": "not-performed",
            "remaining_gates": [*result["remaining_gates"], "installed-client-lifecycle"],
        }

    def drain(self) -> int:
        self.sources.preflight(self.lifecycle.binding)
        return self.lifecycle.drain()


def compose_local_startup(
    environment: LocalContinuityEnvironment,
    binding: ContinuityBinding,
    source: BoundedContinuitySource,
    *,
    index: SQLiteKnowledgeIndex | None = None,
) -> LocalStartupComposition:
    """Validate existing authority before constructing ports; never create missing state."""
    if (
        not isinstance(environment, LocalContinuityEnvironment)
        or not isinstance(binding, ContinuityBinding)
        or not isinstance(source, BoundedContinuitySource)
    ):
        raise ValidationFailed("Typed bounded startup composition inputs required")
    binding.__post_init__()
    if binding.client_id not in {"codex", "claude-code"}:
        raise PolicyViolation("Startup client has no reviewed structural decoder")
    if index is not None and (
        type(index) is not SQLiteKnowledgeIndex or index.read_only is not True
    ):
        raise PolicyViolation("Startup optional index must be exact read-only SQLite index")
    environment.validate(binding)
    operational = SQLiteOperationalStore(environment.operational_path)
    source.probe(operational, binding)
    plan = source.assert_snapshot(operational, binding.source_snapshot_id)
    continuity = SQLiteContinuityStore(environment.operational_path)
    if continuity.get_binding(binding.session_id) != binding:
        raise PolicyViolation("Startup existing exact session binding required")
    project = _BoundedProjectSource(source, plan, binding)
    sources = _CurrentStartupSource(
        continuity,
        project,
        note_sources=SQLiteStartupNoteSource(continuity, KnowledgeFileStore(environment.home)),
        retrieval=None if index is None else LocalStartupRetrieval(index),
        environment=environment,
        checkpoints=SQLiteStartupCheckpointSource(continuity),
    )
    sources.source_capture, sources.operational = source, operational
    continuity.source_resolver = sources
    sources.preflight(binding)
    lifecycle = LocalLifecycleContinuity(
        continuity,
        ClientLifecycleSpool(environment.home, client_id=binding.client_id),
        binding,
        source_probe=lambda: source.probe(operational, binding),
        entry_validator=validate_reviewed_control_entry,
    )
    return LocalStartupComposition(LocalStartupService(lifecycle, sources), lifecycle, sources)
