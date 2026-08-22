"""Sandbox teslim akisi: worktree -> yama -> apply-check -> test -> verifier.

Bu servis authority uretmez. Teslim karari `applied` olsa bile mutation exact
authorization ve runtime claim/receipt zincirinden gecer.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.domain.canonical import digest, digest_of_bytes
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
    assert_main_tree_untouched,
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
    """Worktree yasam dongusu ve yama teslimi."""

    manager: WorktreeManager

    def prepare(self, spec: WorkspaceSpec) -> PreparedWorkspace:
        baseline = fingerprint(self.manager.source_root)
        if baseline.head != spec.source_revision:
            raise PolicyViolation("source revision drift; workspace hazirlanamaz")
        worktree = self.manager.create(spec.workspace_id, revision=spec.source_revision)
        return PreparedWorkspace(spec=spec, worktree=worktree, baseline=baseline)

    def run_tests(
        self, workspace: PreparedWorkspace, specs: tuple[ProcessSpec, ...]
    ) -> tuple[ProcessResult, ...]:
        """Testleri sandbox icinde, shell'siz ve bounded calistirir."""

        return tuple(runner.run(item, cwd=workspace.worktree.path).result for item in specs)

    def build_artifact(
        self,
        workspace: PreparedWorkspace,
        *,
        artifact_id: str,
        now: dt.datetime,
    ) -> tuple[PatchArtifact, str]:
        """Worktree degisikligini yama artifact'ina cevirir."""

        patch = self.manager.diff(workspace.worktree)
        changed = self.manager.changed_paths(workspace.worktree)
        artifact = PatchArtifact(
            artifact_id=artifact_id,
            workspace_id=workspace.spec.workspace_id,
            base_revision=workspace.worktree.revision,
            changed_paths=changed,
            patch_digest=digest_of_bytes(patch.encode("utf-8")),
            created_at=now,
        )
        artifact.assert_within(workspace.spec.policy.allowlist)
        return artifact, patch

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
        """Apply-check, drift ve bagimsiz test kanitini birlikte degerlendirir."""

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
            apply_ok = self.manager.apply_check(patch)
            if not apply_ok:
                outcome, detail = DeliveryOutcome.REJECTED, "git apply --check basarisiz"
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
        assert_main_tree_untouched(before, after)
        return DeliveryReport(
            artifact=artifact,
            decision=decision,
            test_results=test_results,
            main_tree_before=before,
            main_tree_after=after,
        )

    def discard(self, workspace: PreparedWorkspace) -> TreeFingerprint:
        """Calisma alanini kaldirir ve main tree'nin bozulmadigini dogrular."""

        self.manager.remove(workspace.worktree)
        after = fingerprint(self.manager.source_root)
        assert_main_tree_untouched(workspace.baseline, after)
        return after


def default_policy(paths: tuple[str, ...]) -> SandboxPolicy:
    """Yalniz verilen yollara yazabilen, network'u kapali politika."""

    return SandboxPolicy(allowlist=PathAllowlist(paths))
