"""Unit-test dongusu icin yerel production composition root (W07)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from zekam.application.object_store import ObjectStore
from zekam.application.unit_test_agents import (
    AgentSpecialty,
    CanonicalUnitTestAgentGateway,
    UnitTestAgentGateway,
    WorkBinding,
)
from zekam.application.unit_test_ledger import UnitTestLedger
from zekam.application.unit_test_loop import UnitTestLoopService
from zekam.application.unit_test_loop_state import LoopControl
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.domain.unit_test_engineering import UnitTestRequest
from zekam.infrastructure.clients.adapters import opencode_adapter
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_records import CasAssignmentStore, CasRecordIndex
from zekam.infrastructure.unit_test_runner.loop_adapters import (
    MavenEnvironmentObserver,
    MavenUnitTestMeasurer,
)
from zekam.infrastructure.unit_test_runner.patch_workspace import FileSystemTestPatchWorkspace

ArtifactRegistrar = Callable[[str, int, str], None]


@dataclass(frozen=True, slots=True)
class UnitTestRuntimeBinding:
    """Loop'un uyduramayacagi Work/plan/source baglari."""

    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    coordinator_assignment_id: UUID
    project_root: Path
    object_store_root: Path
    lock_dir: Path
    opencode_executable: Path
    approved_maven_plan_digest: str
    target_modules: tuple[str, ...] = ()
    search_path: str | None = None
    allow_network: bool = False
    remote_model: bool = False
    model_id: str | None = None
    plan_id: UUID | None = None
    step_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("project_root", "object_store_root", "lock_dir", "opencode_executable"):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ConfigurationError(f"{name} absolute Path olmali")
        if not self.approved_maven_plan_digest.startswith("sha256:"):
            raise ValidationFailed("approved_maven_plan_digest sha256 olmali")
        if not self.opencode_executable.is_file():
            raise ConfigurationError("OpenCode executable exact dosya olmali")
        if self.allow_network and not self.remote_model:
            raise ValidationFailed("network yalniz explicit remote model baglaminda acilabilir")
        if self.model_id is not None and not self.model_id.strip():
            raise ValidationFailed("model_id bos olamaz")


@dataclass(frozen=True, slots=True)
class UnitTestRuntime:
    """Composition sonucu; caller transaction/commit sinirini yonetir."""

    loop: UnitTestLoopService
    objects: ObjectStore
    gateway: UnitTestAgentGateway


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or len(revision) != 40:
        raise ValidationFailed("git source revision okunamadi")
    return revision


def _git_dirty(root: Path) -> frozenset[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 2_000_000:
        raise ValidationFailed("git dirty durumu okunamadi")
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().replace("\\", "/")
        if path:
            paths.add(path)
    return frozenset(paths)


def compose_unit_test_runtime(
    request: UnitTestRequest,
    *,
    ledger: UnitTestLedger,
    binding: UnitTestRuntimeBinding,
    artifact_registrar: ArtifactRegistrar,
    control: LoopControl | None = None,
) -> UnitTestRuntime:
    """Mevcut operational UoW ve exact binding ile gercek loop adaptorlerini baglar."""

    objects = LocalContentAddressedStore(binding.object_store_root).ensure()
    index = CasRecordIndex(objects)
    assignments = CasAssignmentStore(
        index,
        request.request_digest,
        registrar=artifact_registrar,
    )
    adapter = opencode_adapter(str(binding.opencode_executable), model_id=binding.model_id)
    adapters = dict.fromkeys(AgentSpecialty, adapter)
    gateway = CanonicalUnitTestAgentGateway(
        work=WorkBinding(
            realm_id=binding.realm_id,
            project_id=binding.project_id,
            work_item_id=binding.work_item_id,
            coordinator_assignment_id=binding.coordinator_assignment_id,
            plan_id=binding.plan_id,
            step_id=binding.step_id,
        ),
        store=assignments,
        objects=objects,
        adapters=adapters,
        cwd=binding.project_root,
        is_remote=binding.remote_model,
    )
    measurer = MavenUnitTestMeasurer(
        project_root=binding.project_root,
        target_modules=binding.target_modules,
        lock_dir=binding.lock_dir,
        allow_network=binding.allow_network,
        search_path=binding.search_path,
        approved_plan_digest=binding.approved_maven_plan_digest,
    )
    environment = MavenEnvironmentObserver(
        project_root=binding.project_root,
        target_modules=binding.target_modules,
        source_files=request.source_files,
        revision_provider=lambda: _git_revision(binding.project_root),
        dirty_provider=lambda: _git_dirty(binding.project_root),
        search_path=binding.search_path,
    )
    loop = UnitTestLoopService(
        ledger=ledger,
        objects=objects,
        gateway=gateway,
        measurer=measurer,
        workspace=FileSystemTestPatchWorkspace(binding.project_root),
        environment=environment,
        control=control,
    )
    return UnitTestRuntime(loop=loop, objects=objects, gateway=gateway)
