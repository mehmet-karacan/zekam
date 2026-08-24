"""Bound real-source teslim akisi: exact write -> test -> verifier.

Bu servis authority uretmez. Teslim karari `applied` olsa bile mutation exact
authorization ve runtime claim/receipt zincirinden gecer.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.checkpoint_v2 import SandboxBindingV2, SandboxDisposition
from zekam.domain.errors import PolicyViolation
from zekam.domain.sandbox import (
    DeliveryDecision,
    DeliveryOutcome,
    PatchArtifact,
    PathAllowlist,
    ProcessResult,
    ProcessSpec,
    SandboxPolicy,
    TreeFingerprint,
    WorkspaceSpec,
    assert_no_drift,
)
from zekam.infrastructure.git.worktree import ManagedWorktree, WorktreeManager, fingerprint
from zekam.infrastructure.process import runner


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    """Acilmis ve politikaya baglanmis calisma alani."""

    spec: WorkspaceSpec
    worktree: ManagedWorktree
    baseline: TreeFingerprint

    def resolve_write(self, relative: str) -> Path:
        """Yazma hedefini allowlist ve symlink kacisina karsi dogrular."""

        self.spec.policy.allowlist.assert_permits(relative)
        return self.worktree.resolve(relative)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.spec.workspace_id,
            "spec_digest": self.spec.spec_digest,
            "revision": self.worktree.revision,
            "baseline": self.baseline.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """Teslim sonucunun tam kaniti."""

    artifact: PatchArtifact
    decision: DeliveryDecision
    test_results: tuple[ProcessResult, ...]
    main_tree_before: TreeFingerprint
    main_tree_after: TreeFingerprint

    @property
    def receipt_eligible(self) -> bool:
        """Terminal receipt yalniz applied teslim icin yazilabilir."""

        return self.decision.outcome is DeliveryOutcome.APPLIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact.body(),
            "decision": self.decision.as_dict(),
            "test_results": [item.as_dict() for item in self.test_results],
            "main_tree_before": self.main_tree_before.as_dict(),
            "main_tree_after": self.main_tree_after.as_dict(),
            "receipt_eligible": self.receipt_eligible,
        }

    @property
    def report_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class SandboxDeliveryService:
    """Bagli gercek source rootu teslim yasam dongusu."""

    manager: WorktreeManager
    resolve_bound_source: Callable[[str], Path]

    def prepare(self, spec: WorkspaceSpec) -> PreparedWorkspace:
        try:
            registered = self.resolve_bound_source(spec.project_ref).resolve(strict=True)
            configured = self.manager.source_root.resolve(strict=True)
        except OSError as exc:
            raise PolicyViolation("project_ref icin exact source binding cozumlenemedi") from exc
        if registered != configured:
            raise PolicyViolation("project_ref exact source binding ile eslesmiyor")
        baseline = fingerprint(self.manager.source_root)
        if baseline.head != spec.source_revision:
            raise PolicyViolation("source revision drift; workspace hazirlanamaz")
        worktree = self.manager.create(spec.workspace_id, revision=spec.source_revision)
        return PreparedWorkspace(spec=spec, worktree=worktree, baseline=baseline)

    def run_tests(
        self, workspace: PreparedWorkspace, specs: tuple[ProcessSpec, ...]
    ) -> tuple[ProcessResult, ...]:
        """Testleri bagli source rootunda, shell'siz ve bounded calistirir."""

        return tuple(runner.run(item, cwd=workspace.worktree.path).result for item in specs)

    def build_artifact(
        self,
        workspace: PreparedWorkspace,
        *,
        artifact_id: str,
        now: dt.datetime,
    ) -> tuple[PatchArtifact, str]:
        """Gercek source degisikligini kanit artifact'ina cevirir."""

        patch = self.manager.diff(workspace.worktree)
        changed = self.manager.changed_paths(workspace.worktree)
        artifact = PatchArtifact(
            artifact_id=artifact_id,
            workspace_id=workspace.spec.workspace_id,
            base_revision=workspace.worktree.revision,
            changed_paths=changed,
            patch_digest=self.manager.patch_digest(workspace.worktree),
            created_at=now,
        )
        artifact.assert_within(workspace.spec.policy.allowlist)
        return artifact, patch

    def checkpoint_binding(
        self,
        workspace: PreparedWorkspace,
        *,
        artifact: PatchArtifact | None = None,
    ) -> SandboxBindingV2:
        """Canli bound-source durumunu checkpoint v2 binding'ine derler."""

        current = fingerprint(workspace.worktree.path)
        if current.head != workspace.worktree.revision:
            raise PolicyViolation("sandbox checkpoint base revision drift")
        if not current.dirty:
            if artifact is not None:
                raise PolicyViolation("clean sandbox patch artifact tasiyamaz")
            return SandboxBindingV2(
                SandboxDisposition.CLEAN,
                workspace.spec.workspace_id,
                workspace.worktree.revision,
            )
        if artifact is None:
            raise PolicyViolation("dirty sandbox exact patch artifact ister")
        if (
            artifact.workspace_id != workspace.spec.workspace_id
            or artifact.base_revision != workspace.worktree.revision
            or artifact.changed_paths != self.manager.changed_paths(workspace.worktree)
            or artifact.patch_digest != self.manager.patch_digest(workspace.worktree)
        ):
            raise PolicyViolation("sandbox checkpoint patch provenance drift")
        return SandboxBindingV2(
            SandboxDisposition.DIRTY,
            workspace.spec.workspace_id,
            workspace.worktree.revision,
            artifact.patch_digest,
            self.manager.dirty_state_digest(workspace.worktree),
        )

    def assert_checkpoint_binding(self, binding: SandboxBindingV2) -> None:
        """Resume apply oncesi checkpoint binding'ini canli source-root ile dogrular."""

        if binding.disposition is SandboxDisposition.NOT_APPLICABLE:
            raise PolicyViolation("bound-source resume sandbox binding ister")
        if binding.sandbox_id is None or binding.base_revision is None:
            raise PolicyViolation("resume sandbox identity ve base revision ister")
        worktree = ManagedWorktree(
            workspace_id=binding.sandbox_id,
            path=self.manager.source_root,
            revision=binding.base_revision,
        )
        current = fingerprint(worktree.path)
        if current.head != binding.base_revision:
            raise PolicyViolation("resume sandbox base revision drift")
        if binding.disposition is SandboxDisposition.CLEAN:
            if current.dirty:
                raise PolicyViolation("resume clean sandbox dirty state drift")
            return
        if (
            not current.dirty
            or binding.patch_digest != self.manager.patch_digest(worktree)
            or binding.dirty_state_digest != self.manager.dirty_state_digest(worktree)
        ):
            raise PolicyViolation("resume sandbox patch veya dirty state drift")

    @contextmanager
    def hold_checkpoint_binding(self, binding: SandboxBindingV2) -> Iterator[None]:
        """Source-root exclusive lease'i live validation ve effect boyunca tutar."""

        if binding.sandbox_id is None or binding.base_revision is None:
            raise PolicyViolation("resume sandbox identity ve base revision ister")
        lease = self.manager.create(binding.sandbox_id, revision=binding.base_revision)
        try:
            self.assert_checkpoint_binding(binding)
            yield
        finally:
            self.manager.remove(lease)

    def deliver(
        self,
        workspace: PreparedWorkspace,
        *,
        artifact: PatchArtifact,
        patch: str,
        planned_paths: tuple[str, ...],
        test_results: tuple[ProcessResult, ...],
        builder_ref: str,
        verifier_ref: str,
    ) -> DeliveryReport:
        """Direct-source drift ve bagimsiz test kanitini birlikte degerlendirir."""

        before = fingerprint(self.manager.source_root)
        tests_passed = bool(test_results) and all(item.succeeded for item in test_results)

        outcome = DeliveryOutcome.APPLIED
        detail = ""
        apply_ok = False
        try:
            assert_no_drift(
                planned_revision=workspace.spec.source_revision,
                current_revision=before.head,
                planned_paths=planned_paths,
                changed_paths=artifact.changed_paths,
            )
        except PolicyViolation as exc:
            outcome, detail = DeliveryOutcome.DRIFTED, str(exc)
        else:
            apply_ok = (
                bool(artifact.changed_paths)
                and patch == self.manager.diff(workspace.worktree)
                and artifact.patch_digest == self.manager.patch_digest(workspace.worktree)
                and artifact.changed_paths == self.manager.changed_paths(workspace.worktree)
            )
            if not apply_ok:
                outcome, detail = DeliveryOutcome.REJECTED, "degisiklik kaniti drift veya bos"
            elif not tests_passed:
                outcome, detail = DeliveryOutcome.REJECTED, "bagimsiz test kaniti yok"

        decision = DeliveryDecision(
            artifact_digest=artifact.artifact_digest,
            outcome=outcome,
            apply_check_passed=apply_ok,
            tests_passed=tests_passed,
            verifier_ref=verifier_ref,
            builder_ref=builder_ref,
            detail=detail,
        )
        after = fingerprint(self.manager.source_root)
        return DeliveryReport(
            artifact=artifact,
            decision=decision,
            test_results=test_results,
            main_tree_before=workspace.baseline,
            main_tree_after=after,
        )

    def discard(self, workspace: PreparedWorkspace) -> TreeFingerprint:
        """Kopya kaldirmadan bagli source tree'nin son parmak izini dondurur."""

        self.manager.remove(workspace.worktree)
        after = fingerprint(self.manager.source_root)
        return after


def default_policy(paths: tuple[str, ...]) -> SandboxPolicy:
    """Yalniz verilen yollara yazabilen, network'u kapali politika."""

    return SandboxPolicy(allowlist=PathAllowlist(paths))
